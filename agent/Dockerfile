FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN adduser --disabled-password --no-create-home agentuser

COPY agent/ agent/

USER agentuser

CMD ["python", "-m", "agent.main"]
