FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*
COPY partywin-stage8-staging-qa.zip /tmp/partywin.zip
RUN unzip -q /tmp/partywin.zip -d /tmp/partywin-src \
    && cp -a /tmp/partywin-src/partywin-stage8-staging-qa/. /app/ \
    && rm -rf /tmp/partywin.zip /tmp/partywin-src
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8000
CMD ["sh","-c","python -m uvicorn server:app --host 0.0.0.0 --port ${PORT}"]
