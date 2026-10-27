FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 10001 -m xmu
COPY xmu xmu
RUN mkdir /data && chown xmu:xmu /data
USER xmu
ENV XMU_DATA_DIR=/data
EXPOSE 7321
CMD ["python", "-m", "uvicorn", "xmu.web:create_app", "--factory", "--host", "0.0.0.0", "--port", "7321"]
