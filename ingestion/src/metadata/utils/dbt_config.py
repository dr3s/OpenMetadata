#  Copyright 2021 Collate
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
"""
Hosts the singledispatch to get DBT files
"""
import json
import traceback
from functools import singledispatch
from typing import Optional, Tuple

import requests

from metadata.generated.schema.metadataIngestion.databaseServiceMetadataPipeline import (
    DbtCloudConfig,
    DbtGCSConfig,
    DbtHttpConfig,
    DbtLocalConfig,
    DbtS3Config,
)
from metadata.ingestion.ometa.utils import ometa_logger
from metadata.utils.credentials import set_google_credentials

logger = ometa_logger()

DBT_CATALOG_FILE_NAME = "catalog.json"
DBT_MANIFEST_FILE_NAME = "manifest.json"
DBT_RUN_RESULTS_FILE_NAME = "run_results.json"


@singledispatch
def get_dbt_details(config):
    if config:
        raise NotImplementedError(
            f"Config not implemented for type {type(config)}: {config}"
        )


@get_dbt_details.register
def _(config: DbtLocalConfig):
    try:
        dbt_run_results = None
        if config.dbtCatalogFilePath is not None:
            logger.debug(
                f"Reading [dbtCatalogFilePath] from: {config.dbtCatalogFilePath}"
            )
            with open(config.dbtCatalogFilePath, "r", encoding="utf-8") as catalog:
                dbt_catalog = catalog.read()
        if config.dbtManifestFilePath is not None:
            logger.debug(
                f"Reading [dbtManifestFilePath] from: {config.dbtCatalogFilePath}"
            )
            with open(config.dbtManifestFilePath, "r", encoding="utf-8") as manifest:
                dbt_manifest = manifest.read()
        if (
            config.dbtRunResultsFilePath is not None
            and config.dbtRunResultsFilePath is not ""
        ):
            logger.debug(
                f"Reading [dbtRunResultsFilePath] from: {config.dbtRunResultsFilePath}"
            )
            with open(
                config.dbtRunResultsFilePath, "r", encoding="utf-8"
            ) as run_results:
                dbt_run_results = run_results.read()
        return (
            json.loads(dbt_catalog),
            json.loads(dbt_manifest),
            json.loads(dbt_run_results) if dbt_run_results else None,
        )
    except Exception as exc:
        logger.debug(traceback.format_exc())
        logger.warning(f"Error fetching dbt files from local: {exc}")
    return None


@get_dbt_details.register
def _(config: DbtHttpConfig):
    try:
        logger.debug(f"Requesting [dbtCatalogHttpPath] to: {config.dbtCatalogHttpPath}")
        dbt_catalog = requests.get(config.dbtCatalogHttpPath)
        logger.debug(f"Requesting [dbtCatalogHttpPath] to: {config.dbtCatalogHttpPath}")
        dbt_manifest = requests.get(config.dbtManifestHttpPath)
        dbt_run_results = None
        if (
            config.dbtRunResultsHttpPath is not None
            and config.dbtRunResultsHttpPath is not ""
        ):
            logger.debug(
                f"Requesting [dbtRunResultsHttpPath] to: {config.dbtRunResultsHttpPath}"
            )
            dbt_run_results = requests.get(config.dbtRunResultsHttpPath)
        return (
            json.loads(dbt_catalog.text),
            json.loads(dbt_manifest.text),
            json.loads(dbt_run_results.text) if dbt_run_results else None,
        )
    except Exception as exc:
        logger.debug(traceback.format_exc())
        logger.warning(f"Error fetching dbt files from file server: {exc}")
    return None


@get_dbt_details.register
def _(config: DbtCloudConfig):
    dbt_catalog = None
    dbt_manifest = None
    dbt_run_results = None
    try:
        from metadata.ingestion.ometa.client import REST, ClientConfig

        expiry = 0
        auth_token = config.dbtCloudAuthToken.get_secret_value(), expiry
        client_config = ClientConfig(
            base_url="https://cloud.getdbt.com",
            api_version="api/v2",
            auth_token=lambda: auth_token,
            auth_header="Authorization",
            allow_redirects=True,
        )
        client = REST(client_config)
        account_id = config.dbtCloudAccountId
        logger.debug(
            "Requesting [dbt_catalog], [dbt_manifest] and [dbt_run_results] data"
        )
        response = client.get(
            f"/accounts/{account_id}/runs/?order_by=-finished_at&limit=1"
        )
        runs_data = response.get("data")
        if runs_data:
            run_id = runs_data[0]["id"]
            logger.debug(f"Requesting [dbt_catalog]")
            dbt_catalog = client.get(
                f"/accounts/{account_id}/runs/{run_id}/artifacts/{DBT_CATALOG_FILE_NAME}"
            )
            logger.debug(f"Requesting [dbt_manifest]")
            dbt_manifest = client.get(
                f"/accounts/{account_id}/runs/{run_id}/artifacts/{DBT_MANIFEST_FILE_NAME}"
            )
            logger.debug(f"Requesting [dbt_run_results]")
            dbt_run_results = client.get(
                f"/accounts/{account_id}/runs/{run_id}/artifacts/{DBT_RUN_RESULTS_FILE_NAME}"
            )
    except Exception as exc:
        logger.debug(traceback.format_exc())
        logger.warning(f"Error fetching dbt files from DBT Cloud: {exc}")
    return dbt_catalog, dbt_manifest, dbt_run_results


@get_dbt_details.register
def _(config: DbtS3Config):
    dbt_catalog = None
    dbt_manifest = None
    dbt_run_results = None
    try:
        bucket_name, prefix = get_dbt_prefix_config(config)
        from metadata.clients.aws_client import AWSClient

        aws_client = AWSClient(config.dbtSecurityConfig).get_resource("s3")

        if not bucket_name:
            buckets = aws_client.buckets.all()
        else:
            buckets = [aws_client.Bucket(bucket_name)]
        for bucket in buckets:
            if prefix:
                obj_list = bucket.objects.filter(Prefix=prefix)
            else:
                obj_list = bucket.objects.all()
            for bucket_object in obj_list:
                if DBT_MANIFEST_FILE_NAME in bucket_object.key:
                    logger.debug(f"{DBT_MANIFEST_FILE_NAME} found")
                    dbt_manifest = bucket_object.get()["Body"].read().decode()
                if DBT_CATALOG_FILE_NAME in bucket_object.key:
                    logger.debug(f"{DBT_CATALOG_FILE_NAME} found")
                    dbt_catalog = bucket_object.get()["Body"].read().decode()
                if DBT_RUN_RESULTS_FILE_NAME in bucket_object.key:
                    logger.debug(f"{DBT_RUN_RESULTS_FILE_NAME} found")
                    dbt_run_results = bucket_object.get()["Body"].read().decode()
        return (
            json.loads(dbt_catalog),
            json.loads(dbt_manifest),
            json.loads(dbt_run_results) if dbt_run_results else None,
        )
    except Exception as exc:
        logger.debug(traceback.format_exc())
        logger.warning(f"Error fetching dbt files from s3: {exc}")
    return dbt_catalog, dbt_manifest


@get_dbt_details.register
def _(config: DbtGCSConfig):
    dbt_catalog = None
    dbt_manifest = None
    dbt_run_results = None
    try:
        bucket_name, prefix = get_dbt_prefix_config(config)
        from google.cloud import storage

        set_google_credentials(gcs_credentials=config.dbtSecurityConfig)
        client = storage.Client()
        if not bucket_name:
            buckets = client.list_buckets()
        else:
            buckets = [client.get_bucket(bucket_name)]
        for bucket in buckets:
            if prefix:
                obj_list = client.list_blobs(bucket.name, prefix=prefix)
            else:
                obj_list = client.list_blobs(bucket.name)
            for blob in obj_list:
                if DBT_MANIFEST_FILE_NAME in blob.name:
                    logger.debug(f"{DBT_MANIFEST_FILE_NAME} found")
                    dbt_manifest = blob.download_as_string().decode()
                if DBT_CATALOG_FILE_NAME in blob.name:
                    logger.debug(f"{DBT_CATALOG_FILE_NAME} found")
                    dbt_catalog = blob.download_as_string().decode()
                if DBT_RUN_RESULTS_FILE_NAME in blob.name:
                    logger.debug(f"{DBT_RUN_RESULTS_FILE_NAME} found")
                    dbt_run_results = blob.download_as_string().decode()
        return (
            json.loads(dbt_catalog),
            json.loads(dbt_manifest),
            json.loads(dbt_run_results) if dbt_run_results else None,
        )
    except Exception as exc:
        logger.debug(traceback.format_exc())
        logger.warning(f"Error fetching dbt files from gcs: {exc}")
    return dbt_catalog, dbt_manifest


def get_dbt_prefix_config(config) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (bucket, prefix) tuple
    """
    if config.dbtPrefixConfig:
        return (
            config.dbtPrefixConfig.dbtBucketName,
            config.dbtPrefixConfig.dbtObjectPrefix,
        )
    return None, None
