FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shim.py .

RUN mkdir -p /data

EXPOSE 8300

CMD ["python3", "shim.py"]
