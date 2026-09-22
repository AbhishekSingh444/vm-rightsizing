FROM python:3.12-slim

# No third-party dependencies: the agent uses only the standard library,
# so the image stays small and builds offline inside kind.
WORKDIR /app
COPY replay_agent.py /app/replay_agent.py

RUN mkdir -p /var/log /trace

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/app/replay_agent.py"]
