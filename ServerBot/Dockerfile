FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Copy dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browsers for Playwright before copying the app code so this heavy
# layer (downloads Chromium) stays cached across code changes. It only re-runs
# when the base image or requirements above change.
RUN playwright install chromium

# Copy the rest of the code
COPY . .

# Expose Flask port
EXPOSE 3267

# Start the Flask app
CMD ["python", "app.py"]
