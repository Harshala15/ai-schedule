FROM public.ecr.aws/lambda/python:3.12

ENV PYTHONUNBUFFERED=1 \
    SIMOUR_STORAGE_ROOT=/tmp/bhupalpally \
    PLANT_NAME=BHUPALPALLY \
    HISTORIC_CASES_DIR=/tmp/bhupalpally/historic_cases/BHUPALPALLY \
    ENABLE_S3_STATE_SYNC=1

WORKDIR ${LAMBDA_TASK_ROOT}

RUN pip install --no-cache-dir \
    boto3 \
    numpy \
    opencv-python-headless \
    google-genai \
    -t ${LAMBDA_TASK_ROOT}

COPY config.py daily_feedback.py feature_builder.py image_feature_extraction.py llm_predictor.py \
     physics_anchor.py prediction_store.py run_pipeline.py similarity_retrieval.py \
     state_sync.py time_features.py validator.py video_motion_features.py ${LAMBDA_TASK_ROOT}/
COPY simour_forecast_scheduler/ ${LAMBDA_TASK_ROOT}/simour_forecast_scheduler/

CMD ["simour_forecast_scheduler.handler.lambda_handler"]
