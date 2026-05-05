FROM docker.arvancloud.ir/python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirror-pypi.runflare.com/simple \
    PIP_TRUSTED_HOST=mirror-pypi.runflare.com

RUN python -m pip install --upgrade pip

RUN set -eux; \
    sed -i 's|http://deb.debian.org/debian|https://mirror-linux.runflare.com/debian|g' /etc/apt/sources.list.d/debian.sources; \
    sed -i 's|http://security.debian.org/debian-security|https://mirror-linux.runflare.com/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get update || ( \
      sed -i 's|https://mirror-linux.runflare.com/debian|http://deb.debian.org/debian|g' /etc/apt/sources.list.d/debian.sources; \
      sed -i 's|https://mirror-linux.runflare.com/debian-security|http://security.debian.org/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
      apt-get update \
    ); \
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
