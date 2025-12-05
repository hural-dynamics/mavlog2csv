# -*- coding: utf-8 -*-
"""
API REST para conversão de logs MAVLink para CSV.
Expõe a funcionalidade do mavlog2csv através de uma interface HTTP.

Suporta dois fluxos:
1. Upload direto (arquivos < 32MB): POST /convert
2. Upload via Cloud Storage (arquivos grandes): POST /upload-url + POST /convert-from-gcs
"""
import logging
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

try:
    from google.cloud import storage
    from google.cloud.exceptions import GoogleCloudError

    GCS_AVAILABLE = True
except ImportError:
    GCS_AVAILABLE = False
    storage = None
    GoogleCloudError = Exception

import mavlog2csv

logger = logging.getLogger(__name__)

# Configuração do Cloud Storage
GCS_BUCKET = os.getenv("GCS_BUCKET", "mavlog2csv-uploads")
GCS_EXPIRATION_HOURS = int(os.getenv("GCS_EXPIRATION_HOURS", "1"))

# Inicializar cliente GCS se disponível
storage_client: Optional[storage.Client] = None
if GCS_AVAILABLE:
    try:
        storage_client = storage.Client()
        logger.info(f"Cloud Storage inicializado. Bucket: {GCS_BUCKET}")
    except Exception as e:
        logger.warning(f"Cloud Storage não disponível: {e}")
        storage_client = None

app = FastAPI(
    title="MAVLog2CSV API",
    description="API para conversão de logs ArduPilot (MAVLink) para CSV",
    version="1.0.0",
)


# Modelos Pydantic
class UploadUrlRequest(BaseModel):
    """Request para gerar URL de upload"""

    filename: str
    content_type: str = "application/octet-stream"


class UploadUrlResponse(BaseModel):
    """Response com URL assinada para upload"""

    upload_url: str
    gcs_path: str
    expires_in: int


@app.get("/")
async def root():
    """Endpoint de health check"""
    return {"status": "ok", "service": "mavlog2csv-api"}


@app.get("/health")
async def health():
    """Endpoint de health check detalhado"""
    health_status = {
        "status": "healthy",
        "service": "mavlog2csv-api",
        "gcs_available": GCS_AVAILABLE and storage_client is not None,
    }
    return health_status


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


@app.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(request: UploadUrlRequest):
    """
    Gera uma URL assinada para upload direto ao Cloud Storage.

    Este endpoint permite que o frontend faça upload de arquivos grandes
    diretamente para o Cloud Storage, contornando o limite de 32MB do Cloud Run.

    Fluxo:
    1. Frontend chama este endpoint com o nome do arquivo
    2. API retorna URL assinada temporária
    3. Frontend faz PUT direto para essa URL (não passa pelo Cloud Run)
    4. Frontend chama /convert-from-gcs com o gcs_path retornado

    Args:
        request: Informações do arquivo a ser enviado

    Returns:
        URL assinada, caminho no GCS e tempo de expiração
    """
    if not GCS_AVAILABLE or storage_client is None:
        raise HTTPException(
            status_code=503,
            detail="Cloud Storage não está disponível. Verifique as configurações.",
        )

    file_ext = Path(request.filename).suffix.lower()
    if file_ext not in [".bin", ".log", ".tlog"]:
        raise HTTPException(
            status_code=400,
            detail=f"Formato de arquivo não suportado: {file_ext}. Use .bin, .log ou .tlog",
        )

    try:
        # Gera nome único para o arquivo
        unique_id = str(uuid.uuid4())
        timestamp = datetime.utcnow().strftime("%Y%m%d")
        gcs_path = f"uploads/{timestamp}/{unique_id}{file_ext}"

        bucket = storage_client.bucket(GCS_BUCKET)
        blob = bucket.blob(gcs_path)

        # Gera URL assinada para upload (válida por GCS_EXPIRATION_HOURS)
        expires_at = datetime.utcnow() + timedelta(hours=GCS_EXPIRATION_HOURS)
        upload_url = blob.generate_signed_url(
            expiration=expires_at,
            method="PUT",
            content_type=request.content_type,
        )

        logger.info(f"URL assinada gerada para: {gcs_path}")

        return UploadUrlResponse(
            upload_url=upload_url,
            gcs_path=gcs_path,
            expires_in=GCS_EXPIRATION_HOURS * 3600,
        )

    except GoogleCloudError as e:
        logger.error(f"Erro ao gerar URL assinada: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Erro ao gerar URL de upload: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Erro inesperado: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Erro inesperado: {str(e)}")


@app.post("/convert-from-gcs")
async def convert_log_from_gcs(
    gcs_path: str = Form(..., description="Caminho do arquivo no GCS (retornado por /upload-url)"),
    columns: List[str] = Form(
        ...,
        description="Colunas no formato MessageType.Column. DataFlash: GPS.Lat. TLOG: GLOBAL_POSITION_INT.lat",
    ),
    skip_n_arms: int = Form(0, description="Número de eventos ARM a pular antes de processar"),
):
    """
    Converte um arquivo que já está no Cloud Storage para CSV.

    Use este endpoint após fazer upload via /upload-url.
    Este fluxo permite processar arquivos maiores que 32MB.

    Args:
        gcs_path: Caminho do arquivo no GCS (retornado por /upload-url)
        columns: Lista de colunas no formato MessageType.Column
            - DataFlash (.bin/.log): GPS.Lat, GPS.Lng, ARSP.Airspeed, ATT.Roll
            - TLOG (.tlog): GLOBAL_POSITION_INT.lat, GLOBAL_POSITION_INT.lon, ATTITUDE.roll
        skip_n_arms: Número de eventos ARM a pular (padrão: 0, apenas para DataFlash)

    Returns:
        Arquivo CSV como resposta HTTP
    """
    if not GCS_AVAILABLE or storage_client is None:
        raise HTTPException(
            status_code=503,
            detail="Cloud Storage não está disponível. Verifique as configurações.",
        )

    # Valida formato
    file_ext = Path(gcs_path).suffix.lower()
    if file_ext not in [".bin", ".log", ".tlog"]:
        raise HTTPException(
            status_code=400,
            detail=f"Formato de arquivo não suportado: {file_ext}. Use .bin, .log ou .tlog",
        )

    # Valida colunas
    if not columns:
        raise HTTPException(
            status_code=400, detail="Pelo menos uma coluna deve ser especificada"
        )

    for col in columns:
        try:
            mavlog2csv.parse_cli_column(col)
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Formato de coluna inválido: {col}. {str(e)}"
            )

    tmp_file_path: Optional[str] = None
    csv_file_path: Optional[str] = None

    try:
        # Download do GCS
        bucket = storage_client.bucket(GCS_BUCKET)
        blob = bucket.blob(gcs_path)

        if not blob.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Arquivo não encontrado no GCS: {gcs_path}",
            )

        logger.info(f"Baixando arquivo do GCS: {gcs_path}")

        # Download para arquivo temporário
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            blob.download_to_file(tmp_file)
            tmp_file_path = tmp_file.name

        # Cria arquivo CSV temporário
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv") as csv_file:
            csv_file_path = csv_file.name

        # Chama a função principal (sem modificá-la)
        logger.info(f"Processando arquivo: {gcs_path}")
        mavlog2csv.mavlog2csv(
            device=tmp_file_path,
            columns=columns,
            output=csv_file_path,
            skip_n_arms=skip_n_arms,
        )

        # Lê o CSV gerado
        with open(csv_file_path, "rb") as f:
            csv_content = f.read()

        # Limpa arquivos temporários
        Path(tmp_file_path).unlink(missing_ok=True)
        Path(csv_file_path).unlink(missing_ok=True)

        # Opcional: limpar arquivo do GCS após processamento
        # Descomente a linha abaixo se quiser limpar automaticamente
        # blob.delete()

        logger.info(f"Conversão concluída: {gcs_path}")

        return Response(
            content=csv_content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{Path(gcs_path).stem}.csv"'
            },
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except GoogleCloudError as e:
        logger.error(f"Erro ao acessar Cloud Storage: {str(e)}", exc_info=True)
        if tmp_file_path:
            Path(tmp_file_path).unlink(missing_ok=True)
        if csv_file_path:
            Path(csv_file_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"Erro ao acessar Cloud Storage: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Erro ao processar arquivo: {str(e)}", exc_info=True)
        if tmp_file_path:
            Path(tmp_file_path).unlink(missing_ok=True)
        if csv_file_path:
            Path(csv_file_path).unlink(missing_ok=True)
        raise HTTPException(
            status_code=500, detail=f"Erro ao processar arquivo: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

