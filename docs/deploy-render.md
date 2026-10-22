# Deploy BITS-helper on Render

The app requires more than `pip install .`: chat reads a searchable SQLite
document index before contacting Groq, and PDF indexing/uploads require
Poppler (`pdftotext` and `pdfinfo`). The repository intentionally excludes local
processed data, account databases, and secrets.

## Deployment

1. Commit the Dockerfile, .dockerignore, render.yaml, and application fixes to
   the GitHub branch connected to Render.
2. Create a Render Blueprint from this repository using render.yaml, or create
   a Web Service with **Language: Docker**, repository root as its root
   directory, and **Dockerfile Path: ./Dockerfile**. For an existing service,
   switch its runtime to Docker if available; otherwise create a Docker Web
   Service from the same repository.
3. Leave any Docker Command override empty so Render uses the Dockerfile's CMD.
   The previous native Python build/start commands are not needed.
4. Set GROQ_API_KEY to the actual Groq key, GROQ_MODEL to
   openai/gpt-oss-20b, and APP_COOKIE_SECURE to true. Keep DEMO_MODE false.
5. Deploy and wait for the library build to finish. The image installs Poppler
   and Tesseract, builds document-corpus.sqlite from data/raw, and prepares
   review-bundle.json. The built image includes these generated files.
6. Open the public HTTPS URL, continue as guest, and test Chat. Enter submits
   a question; Shift+Enter inserts a new line. Saved-account Sign out and
   guest Forget this session both invalidate their sessions.

The build never publishes unreviewed rules as an active recommendation dataset.
The free service's writable account/profile data can be lost on redeploy or
restart; use persistent storage for saved accounts in a lasting deployment.

## Troubleshooting chat

The previous generic chat error hid exceptions. The server now logs the real
failure in Render's Logs tab. A missing corpus reports that the reference library
is not ready and keeps the question in the form. If chat still fails after a
successful Docker build, check the logged exception for a missing API key, Groq
authentication/model errors, rate limits, or connection problems. Do not paste
API keys into bug reports.

To build the library locally with Poppler and Tesseract installed:

```sh
pip install .
bits-recommender build-corpus --input data/raw --output data/processed/document-corpus.sqlite
bits-recommender prepare-review --corpus data/processed/document-corpus.sqlite --index "data/raw/All Courses Handouts.html" --output data/processed/review-bundle.json
```
