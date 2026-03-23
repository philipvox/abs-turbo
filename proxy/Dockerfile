FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY moov_proxy.py .

EXPOSE 13379

CMD ["python", "moov_proxy.py"]
