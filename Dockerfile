FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_COOKIE_SECURE=true

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY app ./app
COPY data/raw ./data/raw

# Build the searchable library from reference documents, without copying local
# account/profile databases or publishing unreviewed academic rules.
RUN bits-recommender build-corpus --input data/raw \
    --output data/processed/document-corpus.sqlite \
    && bits-recommender prepare-review \
    --corpus data/processed/document-corpus.sqlite \
    --index "data/raw/All Courses Handouts.html" \
    --output data/processed/review-bundle.json

EXPOSE 8501
CMD ["sh", "-c", "exec python -u app/dashboard.py --host 0.0.0.0 --port \"${PORT:-8501}\""]
