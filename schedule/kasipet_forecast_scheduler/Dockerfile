FROM public.ecr.aws/lambda/python:3.12

ENV PYTHONUNBUFFERED=1 \
    SIMOUR_STORAGE_ROOT=/tmp/kasipet \
    PLANT_NAME=KASIPET \
    HISTORIC_CASES_DIR=/tmp/kasipet/historic_cases/KASIPET \
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
COPY kasipet_forecast_scheduler/ ${LAMBDA_TASK_ROOT}/kasipet_forecast_scheduler/
COPY simour_forecast_scheduler/ ${LAMBDA_TASK_ROOT}/simour_forecast_scheduler/

CMD ["kasipet_forecast_scheduler.lambda_handler.lambda_handler"]
