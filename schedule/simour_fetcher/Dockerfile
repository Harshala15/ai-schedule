FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY simour_fetcher/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY simour_fetcher/ ${LAMBDA_TASK_ROOT}/simour_fetcher/

# Lambda handler entry point
CMD ["simour_fetcher.lambda_handler.lambda_handler"]
