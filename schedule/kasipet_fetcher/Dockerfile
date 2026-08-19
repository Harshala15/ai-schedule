FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

COPY kasipet_fetcher/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -t ${LAMBDA_TASK_ROOT}

COPY kasipet_fetcher/ ${LAMBDA_TASK_ROOT}/kasipet_fetcher/

CMD ["kasipet_fetcher.lambda_handler.lambda_handler"]
