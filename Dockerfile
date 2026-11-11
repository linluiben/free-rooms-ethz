FROM python:3.13-slim
# zoneinfo needs tz data; slim images don't always ship it
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY freerooms ./freerooms
ENV PYTHONUNBUFFERED=1 \
    FREEROOMS_DB=/data/freerooms.sqlite \
    FREEROOMS_HOST=0.0.0.0 \
    FREEROOMS_PORT=8000 \
    FREEROOMS_REFRESH_HOURS=2
VOLUME /data
EXPOSE 8000
USER 1000:1000
CMD ["python", "-m", "freerooms", "serve"]
