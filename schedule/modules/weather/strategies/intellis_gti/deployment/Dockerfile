FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY requirements.lambda.txt ./requirements.lambda.txt
RUN pip install --no-cache-dir -r requirements.lambda.txt
RUN pip install --no-cache-dir fastapi uvicorn mangum

COPY . ./

ENV SIMOUR_STORAGE_ROOT=/tmp/intellis_ai_scheduler
ENV OPENMETEO_API_KEY="jbThkFlLZSXZE3CU"

CMD ["modules.weather.strategies.intellis_gti.api.lambda_handler.handler"]
