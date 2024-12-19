FROM python:3.13-bookworm
RUN echo "deb http://deb.debian.org/debian testing main" >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y -t testing libstdc++6
WORKDIR /app
COPY . .
RUN pip install -e .
EXPOSE 8080
ENV HOST="0.0.0.0" \
    PORT=8080
CMD connect4 --host ${HOST} --port ${PORT}
