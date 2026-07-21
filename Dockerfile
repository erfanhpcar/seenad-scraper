FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m pip install --upgrade pip

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
    unixodbc \
    unixodbc-dev \
    tdsodbc \
    freetds-bin \
    freetds-dev \
    ca-certificates; \
    printf '%s\n' \
      '[FreeTDS]' \
      'Description=FreeTDS unixODBC Driver for SQL Server' \
      'Driver=/usr/lib/x86_64-linux-gnu/odbc/libtdsodbc.so' \
      'Setup=/usr/lib/x86_64-linux-gnu/odbc/libtdsS.so' \
      'Threading=1' \
      'UsageCount=1' \
      > /etc/odbcinst.ini; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
