FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md schema.sql ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir .

CMD ["graphatom", "run"]
