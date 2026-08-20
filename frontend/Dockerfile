FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_PORT=8501 \
    BACKEND_URL=http://backend:8000

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY frontend ./frontend

WORKDIR /app/frontend

EXPOSE 8501

CMD ["sh", "-c", "streamlit run main.py --server.address 0.0.0.0 --server.port ${STREAMLIT_SERVER_PORT}"]
