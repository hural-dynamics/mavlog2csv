# -*- coding: utf-8 -*-
"""
Used https://github.com/ArduPilot/pymavlink/blob/master/tools/mavlogdump.py for reference.
Now supports DataFlash logs (.bin/.log) and MAVLink telemetry logs (.tlog).
"""
import argparse
import collections
import contextlib
import csv
import datetime
import logging
import operator
import re
import sys
import textwrap
from typing import IO, Any, ContextManager, Dict, Iterator, List, Optional, Set, Tuple, Union

from pymavlink import mavutil
from pymavlink.CSVReader import CSVReader
from pymavlink.DFReader import DFReader
from pymavlink.dialects.v10.ardupilotmega import MAVLink_message
from pymavlink.mavutil import mavserial


logger = logging.getLogger(__name__)


def is_message_bad(message: Optional[MAVLink_message]) -> bool:
    """Check if message is bad and worth working with"""
    return bool(message is None or (message and message.get_type() == "BAD_DATA"))


def _get_time_us(message: MAVLink_message) -> int:
    """
    Retorna um timestamp em microssegundos independente do tipo de log.
    Ordem de preferência:
      1) campo TimeUS   (DataFlash)
      2) campo time_usec (MAVLink)
      3) campo time_boot_ms (MAVLink)
      4) _timestamp * 1e6  (timestamp do arquivo)
    """
    if hasattr(message, "TimeUS") and message.TimeUS is not None:
        return int(message.TimeUS)

    if hasattr(message, "time_usec") and message.time_usec is not None:
        return int(message.time_usec)

    if hasattr(message, "time_boot_ms") and message.time_boot_ms is not None:
        return int(message.time_boot_ms) * 1000

    # fallback: timestamp do arquivo (segundos)
    if hasattr(message, "_timestamp") and message._timestamp is not None:
        return int(message._timestamp * 1_000_000)

    return 0


@contextlib.contextmanager
def mavlink_connect(device: str):
    """
    Abre conexão MAVLink para:
      - .tlog  -> leitor de TLOG (mavlogfile)
      - .bin/.log/serial/etc -> mavlink_connection padrão (DFReader/serial)
    """
    lower = device.lower()

    if lower.endswith(".tlog"):
        # TLOG de telemetria
        conn = mavutil.mavlogfile(
            device,
            robust_parsing=True,
            notimestamps=False,
        )
        logger.debug(f"Lendo TLOG (mavlogfile) de {device}")
    else:
        # BIN / LOG de DataFlash, porta serial, etc.
        # (sem robust_parsing=True pra não ficar printando 'bad header')
        conn = mavutil.mavlink_connection(device)
        logger.debug(f"Conectando (mavlink_connection) a {device}")

    try:
        yield conn
    finally:
        logger.debug(f"Fechando conexão com {device}")
        conn.close()


def parse_cli_column(cli_col: str) -> Tuple[str, str]:
    """
    Parse CLI provided column into message type and column name parts.
    """
    match = re.match(r"(?P<message_type>\w+)\.(?P<column>\w+)", cli_col)
    if not match:
        raise ValueError(
            f"""\
            Specified column is not correct format:
            Column "{cli_col}" must be <Message type>.<Column>.
            For example: GPS.Lat  or  GLOBAL_POSITION_INT.lat
        """
        )
    return match.group("message_type"), match.group("column")


def open_output(output: Optional[str] = None) -> ContextManager[IO]:
    """
    Either opens a file `output` for writing or returns STDOUT stream"""
    if output:
        return open(output, "w", newline="")
    else:
        return contextlib.nullcontext(sys.stdout)


def iter_mavlink_messages(device: str, types: Set[str], skip_n_arms: int = 0) -> Iterator[MAVLink_message]:
    """
    Return iterator over mavlink messages of `types` from `device`.
    If skip_n_arms is not zero, will return messages only after skip_n_arms ARM
    events has been seen (EV.Id == 10 in DataFlash logs).
    """
    types = types.copy()
    # EV are DataFlash events like ARM (id=10) or DISARM (id=11).
    # In TLOGs this type usually does not exist, which is fine.
    types.add("EV")
    n_message = 0
    n_armed = 0

    with mavlink_connect(device) as mav_conn:
        while True:
            message: Optional[MAVLink_message] = mav_conn.recv_match(
                blocking=False, type=types
            )
            n_message += 1

            if message is None:
                logger.debug(f"Stopping processing at {n_message} message")
                break

            if is_message_bad(message):
                continue

            if message.get_type() == "EV" and getattr(message, "Id", None) == 10:  # arm event in DF logs
                logger.debug(f"Found ARM event: {message}")
                n_armed += 1

            # For TLOGs, there is usually no EV, so n_armed stays 0 and this
            # condition will be false when skip_n_arms == 0 (default).
            if n_armed < skip_n_arms or message.get_type() == "EV":
                continue

            yield message


# ---------------------------------------------------------------------------
#  Suporte genérico de tempo para DF logs e TLOGs
# ---------------------------------------------------------------------------

def get_message_time(message: MAVLink_message) -> Tuple[int, datetime.datetime]:
    """
    Retorna (TimeUS, datetime) para uma mensagem vinda tanto de DF (.bin/.log)
    quanto de TLOG (.tlog).

    Prioridades:
      1. campo TimeUS (DF logs recentes)
      2. campo time_usec (MAVLink messages – comum em TLOG)
      3. campo time_boot_ms (convertido p/ usec)
      4. message._timestamp (segundos desde epoch, multiplicado por 1e6)
    """
    time_us: Optional[int] = None

    # 1) DataFlash: muitas mensagens têm campo TimeUS diretamente
    raw_time_us = getattr(message, "TimeUS", None)
    if isinstance(raw_time_us, (int, float)) and raw_time_us > 0:
        time_us = int(raw_time_us)

    # 2) MAVLink típico de TLOG: time_usec
    if time_us is None:
        raw_time_usec = getattr(message, "time_usec", None)
        if isinstance(raw_time_usec, (int, float)) and raw_time_usec > 0:
            time_us = int(raw_time_usec)

    # 3) fallback: time_boot_ms
    if time_us is None:
        raw_time_boot_ms = getattr(message, "time_boot_ms", None)
        if isinstance(raw_time_boot_ms, (int, float)) and raw_time_boot_ms >= 0:
            time_us = int(raw_time_boot_ms * 1000)

    # 4) último recurso: _timestamp (segundos desde epoch no arquivo)
    ts = getattr(message, "_timestamp", None)
    if time_us is None:
        if isinstance(ts, (int, float)) and ts > 0:
            time_us = int(ts * 1_000_000)
        else:
            # sem informação nenhuma -> zera
            time_us = 0
            ts = 0

    # constrói datetime a partir do timestamp; se não houver, usa epoch 0
    if not isinstance(ts, (int, float)) or ts <= 0:
        # se tivermos TimeUS mas não timestamp, convertemos para algo razoável
        ts = time_us / 1_000_000.0 if time_us else 0.0

    dt = datetime.datetime.fromtimestamp(ts)

    return time_us, dt


def message_to_row(message: MAVLink_message, columns: List[str]) -> Dict[str, Any]:
    """Convert mavlink message to output row"""
    row: Dict[str, Any] = {}

    time_us = _get_time_us(message)
    row["TimeUS"] = time_us
    row["TimeS"] = round(time_us / 1_000_000, 2)

    dt = datetime.datetime.fromtimestamp(message._timestamp)
    row["Date"] = dt.date().isoformat()
    row["Time"] = dt.time().isoformat()

    for col in columns:
        # se o campo não existir na mensagem, deixa vazio
        col_value = getattr(message, col, None)
        if col_value is None:
            col_value = ""
        row[f"{message.get_type()}.{col}"] = col_value

    return row


def mavlog2csv(device: str, columns: List[str], output: Optional[str] = None, skip_n_arms: int = 0):
    """
    Convert ardupilot telemetry log into csv with selected columns.
    Works with:
      - DataFlash logs: .bin / .log
      - MAVLink telemetry logs: .tlog

    Specify the input file, some desired telemetry columns (like GPS.Lat
    for DF logs or GLOBAL_POSITION_INT.lat for TLOGs), and observe the magic.

    You can find DataFlash message types and their column reference here:
    https://ardupilot.org/copter/docs/logmessages.html

    For TLOGs use MAVLink message names: e.g.
      GLOBAL_POSITION_INT.lat
      GLOBAL_POSITION_INT.lon
      ATTITUDE.roll
    """
    parsed_columns: List[Tuple[str, str]] = list(map(parse_cli_column, columns))

    # Collects all required message types like {'GPS', 'ATT', 'ASPD'}
    # Used to filter mavlink messages
    message_type_filter: Set[str] = set(map(operator.itemgetter(0), parsed_columns))

    # Collects a mapping message type -> columns
    # Used to quickly extract required columns from message
    message_type_columns: Dict[str, List[str]] = collections.defaultdict(list)
    for message_type, column in parsed_columns:
        message_type_columns[message_type].append(column)

    header = [
        "TimeUS",  # time baseline (us) – from DF or synthesized for TLOG
        "TimeS",   # seconds after baseline
        "Date",    # Calculated date of the event
        "Time",    # Calculated time of the event
        *columns,  # User specified columns
    ]

    with open_output(output) as output_file:
        csv_writer = csv.DictWriter(
            output_file,
            fieldnames=header,
            delimiter=",",
            quotechar='"',
            quoting=csv.QUOTE_ALL,
        )
        csv_writer.writeheader()

        for message in iter_mavlink_messages(
            device=device, types=message_type_filter, skip_n_arms=skip_n_arms
        ):
            message_type = message.get_type()
            if message_type in message_type_columns:
                row = message_to_row(message, message_type_columns[message_type])
                csv_writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description=textwrap.dedent(mavlog2csv.__doc__),  # type: ignore
        epilog=textwrap.dedent(
            """\
            Example usage (DataFlash .bin):

              # Output GPS Longitude and latitude and airspeed sensor readings
              python mavlog2csv.py -c GPS.Lng -c GPS.Lat -c ARSP.Airspeed -o output.csv "2023-09-17 13-34-16.bin"

            Example usage (TLOG):

              # Extract lat/lon from MAVLink GLOBAL_POSITION_INT messages
              python mavlog2csv.py -c GLOBAL_POSITION_INT.lat -c GLOBAL_POSITION_INT.lon -o output.csv mission.tlog
        """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("input", help="Input file name (.bin/.log/.tlog).")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file name. If not set, script will output into stdout.",
    )
    parser.add_argument(
        "-c",
        "--col",
        action="append",
        required=True,
        help=(
            "Specify telemetry columns to output. You can specify this multiple times. "
            "Format: <Message type>.<Column>. For example: GPS.Lng or GLOBAL_POSITION_INT.lat"
        ),
    )
    parser.add_argument(
        "--skip-n-arms",
        type=int,
        default=0,
        help=(
            "If there are multiple ARM events in a DataFlash log, skip this number of arms "
            "before writing any rows at all. "
            "Has no effect on TLOGs (they usually don't have EV messages)."
        ),
    )

    args = parser.parse_args()

    mavlog2csv(
        device=args.input,
        columns=args.col,
        skip_n_arms=args.skip_n_arms,
        output=args.output,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    main()