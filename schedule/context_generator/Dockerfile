FROM public.ecr.aws/lambda/python:3.12

WORKDIR ${LAMBDA_TASK_ROOT}

RUN pip install --no-cache-dir boto3 -t ${LAMBDA_TASK_ROOT}

COPY config.py daily_feedback.py state_sync.py time_features.py validator.py feature_builder.py physics_anchor.py prediction_store.py similarity_retrieval.py llm_predictor.py ${LAMBDA_TASK_ROOT}/
COPY modules/ ${LAMBDA_TASK_ROOT}/modules/
COPY context_generator/ ${LAMBDA_TASK_ROOT}/context_generator/

CMD ["context_generator.lambda_handler.lambda_handler"]

