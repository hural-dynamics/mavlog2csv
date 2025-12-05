# Multi-stage build otimizado para Cloud Run
# Stage 1: Build dependencies
FROM python:3.11-slim as builder

WORKDIR /app

# Instala dependências do sistema necessárias para build
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copia requirements e instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Stage 2: Runtime image (otimizado para Cloud Run)
FROM python:3.11-slim

WORKDIR /app

# Copia apenas dependências instaladas do builder
COPY --from=builder /root/.local /root/.local

# Garante que scripts Python estejam no PATH
ENV PATH=/root/.local/bin:$PATH

# Copia código da aplicação
COPY mavlog2csv.py .
COPY api.py .

# Expõe porta padrão do Cloud Run
EXPOSE 8080

# Variáveis de ambiente para otimização
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Comando para iniciar a API
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]

