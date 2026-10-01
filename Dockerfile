FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unzip coreutils && rm -rf /var/lib/apt/lists/*
COPY bundle/ /tmp/bundle/
RUN cat /tmp/bundle/part* | base64 -d > /tmp/partywin.zip \
    && unzip -q /tmp/partywin.zip -d /tmp/partywin-src \
    && cp -a /tmp/partywin-src/partywin-stage8-staging-qa/. /app/ \
    && rm -rf /tmp/partywin.zip /tmp/partywin-src /tmp/bundle
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8000
CMD ["sh","-c","python -m uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
