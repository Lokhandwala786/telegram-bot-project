FROM mcr.microsoft.com/playwright/python:v1.54.0-noble

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQLITE_PATH=/data/jobs.db \
    PORT=8080

WORKDIR /app

# Copy dependency files
COPY requirements.txt requirements-playwright.txt ./

# Install standard requirements and Playwright requirements
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt

# Copy the rest of the application files
COPY . .

# Expose port (useful if stripe webhook or dashboard is enabled)
EXPOSE 8080

# Command to run the bot
CMD ["python", "-m", "app.main", "run"]
