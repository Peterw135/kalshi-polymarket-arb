# Hugging Face Spaces (Docker SDK) — also runs anywhere Docker does.
FROM python:3.12-slim

# Spaces run the container as user 1000; give it a writable app directory.
RUN useradd -m -u 1000 user
WORKDIR /app
COPY --chown=user . /app
RUN mkdir -p /app/data && chown -R user /app/data
USER user

ENV HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["python", "server.py"]
