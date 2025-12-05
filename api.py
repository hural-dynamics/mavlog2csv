# -*- coding: utf-8 -*-
"""
API REST para conversão de logs MAVLink para CSV.
Expõe a funcionalidade do mavlog2csv através de uma interface HTTP.
"""
import logging
import tempfile
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

import mavlog2csv

logger = logging.getLogger(__name__)

app = FastAPI(
    title="MAVLog2CSV API",
    description="API para conversão de logs ArduPilot (MAVLink) para CSV",
    version="1.0.0",
)


@app.get("/")
async def root():
    """Endpoint de health check"""
    return {"status": "ok", "service": "mavlog2csv-api"}


@app.get("/health")
async def health():
    """Endpoint de health check detalhado"""
    return {"status": "healthy", "service": "mavlog2csv-api"}


@app.post("/convert")
async def convert_log(
    file: UploadFile = File(..., description="Arquivo de log (.bin, .log, .tlog)"),
    columns: List[str] = Form(
        ...,
        description="Colunas no formato MessageType.Column. DataFlash: GPS.Lat. TLOG: GLOBAL_POSITION_INT.lat",
    ),
    skip_n_arms: int = Form(0, description="Número de eventos ARM a pular antes de processar"),
):
    """
    Converte um arquivo de log MAVLink para CSV.

    Args:
        file: Arquivo de log (.bin, .log, ou .tlog)
        columns: Lista de colunas no formato MessageType.Column
            - DataFlash (.bin/.log): GPS.Lat, GPS.Lng, ARSP.Airspeed, ATT.Roll
            - TLOG (.tlog): GLOBAL_POSITION_INT.lat, GLOBAL_POSITION_INT.lon, ATTITUDE.roll
        skip_n_arms: Número de eventos ARM a pular (padrão: 0, apenas para DataFlash)

    Returns:
        Arquivo CSV como resposta HTTP
    """
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in [".bin", ".log", ".tlog"]:
        raise HTTPException(
            status_code=400,
            detail=f"Formato de arquivo não suportado: {file_ext}. Use .bin, .log ou .tlog",
        )

    if not columns:
        raise HTTPException(status_code=400, detail="Pelo menos uma coluna deve ser especificada")

    for col in columns:
        try:
            mavlog2csv.parse_cli_column(col)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Formato de coluna inválido: {col}. {str(e)}")

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name

        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv") as csv_file:
            csv_file_path = csv_file.name

        try:
            mavlog2csv.mavlog2csv(
                device=tmp_file_path,
                columns=columns,
                output=csv_file_path,
                skip_n_arms=skip_n_arms,
            )

            with open(csv_file_path, "rb") as f:
                csv_content = f.read()

            Path(tmp_file_path).unlink(missing_ok=True)
            Path(csv_file_path).unlink(missing_ok=True)

            return Response(
                content=csv_content,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="{Path(file.filename).stem}.csv"'
                },
            )

        except Exception as e:
            logger.error(f"Erro ao processar arquivo: {str(e)}", exc_info=True)
            Path(tmp_file_path).unlink(missing_ok=True)
            Path(csv_file_path).unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Erro ao processar arquivo: {str(e)}")

    except Exception as e:
        logger.error(f"Erro ao processar upload: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro ao processar upload: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

