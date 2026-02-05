FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

WORKDIR /app

# Copy dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the code
COPY . .

# Install browsers for Playwright
RUN playwright install chromium

# Expose Flask port
EXPOSE 3267

# Start the Flask app
CMD ["python", "app.py"]
