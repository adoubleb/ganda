FROM debian:bookworm-slim
RUN apt-get update -qq && apt-get install -y -qq ca-certificates >/dev/null && rm -rf /var/lib/apt/lists/*
COPY surreal /surreal
ENV SURREAL_BIND=0.0.0.0:8000
ENTRYPOINT ["/surreal"]
