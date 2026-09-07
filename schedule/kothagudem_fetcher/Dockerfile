FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY kothagudem_fetcher/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY kothagudem_fetcher/ ${LAMBDA_TASK_ROOT}/kothagudem_fetcher/

CMD ["kothagudem_fetcher.lambda_handler.lambda_handler"]
