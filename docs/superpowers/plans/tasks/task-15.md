### Task 15: Dockerfile + packaging check

**Files:**
- Create: `Dockerfile`, `.dockerignore`

- [ ] **Step 1: Write the files**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY auditor ./auditor
COPY webapp ./webapp
RUN pip install --no-cache-dir .
ENV MA_DATA_DIR=/data MA_BIND=0.0.0.0:8484
VOLUME /data
EXPOSE 8484
CMD ["migration-auditor", "serve"]
```

`.dockerignore`:
```
data/
__pycache__/
*.pyc
.git/
tests/
docs/
```

- [ ] **Step 2: Verify the console entry point works**

```bash
python3 -c "from webapp.main import cli; print('entry ok')"
python3 -m pytest -q
```
Expected: `entry ok`, full suite passes.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "chore: Dockerfile (hosting-ready container) + dockerignore"
```

---

