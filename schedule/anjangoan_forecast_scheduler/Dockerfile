FROM kothagudem-forecast-scheduler:latest

ENV PYTHONUNBUFFERED=1 \
    PLANT_NAME=ANJANGOAN \
    PLANT_LAT=21.97583333 \
    PLANT_LON=75.96833333 \
    PLANT_CAPACITY_MW=20.0 \
    PLANT_DC_CAPACITY_MW=26.0 \
    ENABLE_WINDY_VIDEO_FEATURES=0 \
    SIMOUR_STORAGE_ROOT=/tmp/ANJANGOAN \
    ENABLE_S3_STATE_SYNC=1

WORKDIR 

COPY config.py \
     daily_feedback.py \
     feature_builder.py \
     image_feature_extraction.py \
     llm_predictor.py \
     physics_anchor.py \
     prediction_store.py \
     run_pipeline.py \
     similarity_retrieval.py \
     state_sync.py \
     time_features.py \
     validator.py \
     video_motion_features.py \
     /

COPY modules/ /modules/
COPY plant_profiles/ /plant_profiles/
COPY anjangoan_forecast_scheduler/ /anjangoan_forecast_scheduler/
COPY simour_forecast_scheduler/ /simour_forecast_scheduler/

CMD ["anjangoan_forecast_scheduler.lambda_handler.lambda_handler"]

