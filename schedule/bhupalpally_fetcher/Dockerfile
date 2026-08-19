FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY bhupalpally_fetcher/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY bhupalpally_fetcher/ ${LAMBDA_TASK_ROOT}/bhupalpally_fetcher/

CMD ["bhupalpally_fetcher.lambda_handler.lambda_handler"]
