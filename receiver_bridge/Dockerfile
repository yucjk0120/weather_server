FROM python:3.12-slim

WORKDIR /app

# tzdata: 時刻を JST で付けるためのタイムゾーン情報 (slim イメージには入っていない場合がある)
RUN pip install --no-cache-dir pyserial==3.5 tzdata

COPY bridge.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "bridge.py"]
