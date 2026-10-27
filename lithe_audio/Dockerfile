ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY app/requirements.txt .
# Compilers only for architectures without musllinux wheels (armv7).
RUN apk add --no-cache --virtual .build-deps gcc musl-dev python3-dev \
    && pip3 install --no-cache-dir --break-system-packages -r requirements.txt \
    && apk del .build-deps

COPY app/ .

CMD ["python3", "-u", "main.py"]
