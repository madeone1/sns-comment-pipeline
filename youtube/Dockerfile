FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY youtube_validation ./youtube_validation
ENTRYPOINT ["python", "-m", "youtube_validation.worker"]
