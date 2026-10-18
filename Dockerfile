# Airflow image + the Python libraries our pipeline scripts need
FROM apache/airflow:2.10.5-python3.12
RUN pip install --no-cache-dir pandas psycopg2-binary sqlalchemy
