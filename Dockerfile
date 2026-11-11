FROM python:3.12-slim
WORKDIR /app
COPY sns_comment /app/sns_comment
COPY --chown=65532:65532 config.json /app/config.json
ENV PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python", "-m", "sns_comment.cloud"]
