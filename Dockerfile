FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port required by Flask/Gunicorn
EXPOSE 10000

# Start using Gunicorn (since Flask dev server is not production-ready)
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "main:app"]
