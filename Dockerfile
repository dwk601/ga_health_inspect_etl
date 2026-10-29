FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home etl
COPY ga_health_inspect ./ga_health_inspect
COPY schema.sql .
COPY tests ./tests
USER etl
ENTRYPOINT ["python", "-m", "ga_health_inspect"]
CMD ["--max-pages", "2"]
