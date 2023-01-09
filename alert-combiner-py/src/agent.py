# Copyright 2022 The Forta Foundation

import logging
import sys
import threading
from datetime import datetime, timedelta

import forta_agent
import pandas as pd
from forta_agent import get_json_rpc_url
from hexbytes import HexBytes
from web3 import Web3
from forta_agent import FindingSeverity

from src.findings import AlertCombinerFinding
from src.constants import (ENTITY_CLUSTERS_MAX_QUEUE_SIZE, FP_CLUSTERS_QUEUE_MAX_SIZE, BASE_BOTS, ENTITY_CLUSTER_BOT_ALERT_ID, ALERTED_CLUSTERS_MAX_QUEUE_SIZE,
                           FP_MITIGATION_BOTS, ALERTS_LOOKBACK_WINDOW_IN_HOURS, ENTITY_CLUSTER_BOT, ANOMALY_SCORE_THRESHOLD_STRICT, ANOMALY_SCORE_THRESHOLD_LOOSE, LOCAL_NODE,
                           MIN_ALERTS_COUNT, ALERTS_DATA_KEY, ALERTED_CLUSTERS_STRICT_KEY, ALERTED_CLUSTERS_LOOSE_KEY, ENTITY_CLUSTERS_KEY, FP_MITIGATION_CLUSTERS_KEY, 
                           VICTIMS_KEY, VICTIM_QUEUE_MAX_SIZE, VICTIM_IDENTIFICATION_BOT, VICTIM_IDENTIFICATION_BOT_ALERT_IDS, AD_SCORE_ANOMALY_SCORE)
from src.luabase import Luabase, MUTEX_LUABASE
from src.L2Cache import L2Cache

web3 = Web3(Web3.HTTPProvider(get_json_rpc_url()))
luabase = Luabase()

CHAIN_ID = 1

FINDINGS_CACHE = []
CONTRACT_CACHE = dict()  # address -> is_contract
ENTITY_CLUSTERS = dict()  # address -> cluster
ALERTS = []
ALERT_DATA = dict()  # cluster -> pd.DataFrame
ALERTED_CLUSTERS_STRICT = []  # cluster
ALERTED_CLUSTERS_LOOSE = []  # cluster
FP_MITIGATION_CLUSTERS = []  # cluster
VICTIMS = dict()  # transaction_hash, metadata
ALERT_ID_AD_SCORER_MAPPING = dict()  # (bot_id, alert_id) -> ad_scorer
ALERT_ID_STAGE_MAPPING = dict()  # (bot_id, alert_id) -> stage
MUTEX = False

root = logging.getLogger()
root.setLevel(logging.DEBUG)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
root.addHandler(handler)

# TODO - extract label from bot for better aggregation
# TODO - emit an FP alert if FP mitigation came in after the alert
# TODO - add FP mitigation alert to pos reputation bot to look at internal tx count for a given address


def initialize():
    """
    this function initializes the state variables that are tracked across tx and blocks
    it is called from test to reset state between tests
    """
    logging.debug('initializing')

    global CHAIN_ID
    try:
        CHAIN_ID = web3.eth.chain_id
    except Exception as e:
        logging.error(f"Error getting chain id: {e}")
        raise e

    global ALERT_ID_AD_SCORER_MAPPING
    ALERT_ID_AD_SCORER_MAPPING = dict([((bot_id, alert_id), ad_scorer) for bot_id, alert_id, stage, ad_scorer in BASE_BOTS])

    global ALERT_ID_STAGE_MAPPING
    ALERT_ID_STAGE_MAPPING = dict([((bot_id, alert_id), stage) for bot_id, alert_id, stage, ad_scorer in BASE_BOTS])

    global ALERTED_CLUSTERS_STRICT
    alerted_clusters = load(CHAIN_ID, ALERTED_CLUSTERS_STRICT_KEY)
    ALERTED_CLUSTERS_STRICT = [] if alerted_clusters is None else list(alerted_clusters)

    global ALERTED_CLUSTERS_LOOSE
    alerted_clusters = load(CHAIN_ID, ALERTED_CLUSTERS_LOOSE_KEY)
    ALERTED_CLUSTERS_LOOSE = [] if alerted_clusters is None else list(alerted_clusters)

    global ALERT_DATA
    alerts = load(CHAIN_ID, ALERTS_DATA_KEY)
    ALERT_DATA = {} if alerts is None else dict(alerts)

    global ENTITY_CLUSTERS
    entity_cluster_alerts = load(CHAIN_ID, ENTITY_CLUSTERS_KEY)
    ENTITY_CLUSTERS = {} if entity_cluster_alerts is None else dict(entity_cluster_alerts)

    global VICTIMS
    victims = load(CHAIN_ID, VICTIMS_KEY)
    VICTIMS = {} if victims is None else dict(victims)

    global FP_MITIGATION_CLUSTERS
    fp_mitigation_alerts = load(CHAIN_ID, FP_MITIGATION_CLUSTERS_KEY)
    FP_MITIGATION_CLUSTERS = [] if fp_mitigation_alerts is None else list(fp_mitigation_alerts)

    global FINDINGS_CACHE
    FINDINGS_CACHE = []

    global CONTRACT_CACHE
    CONTRACT_CACHE = {}

    global MUTEX
    MUTEX = False

    subscription_json = []
    for bot, alertId, stage, ad_scorer in BASE_BOTS:
        subscription_json.append({"botId": bot, "alertId": alertId})

    for bot, alertId in FP_MITIGATION_BOTS:
        subscription_json.append({"botId": bot, "alertId": alertId})

    subscription_json.append({"botId": ENTITY_CLUSTER_BOT, "alertId": ENTITY_CLUSTER_BOT_ALERT_ID})

    subscription_json.append({"botId": VICTIM_IDENTIFICATION_BOT, "alertId": VICTIM_IDENTIFICATION_BOT_ALERT_IDS[0]})

    subscription_json.append({"botId": VICTIM_IDENTIFICATION_BOT, "alertId": VICTIM_IDENTIFICATION_BOT_ALERT_IDS[1]})

    return {"alertConfig": {"subscriptions": subscription_json}}


def is_contract(w3, addresses) -> bool:
    """
    this function determines whether address/ addresses is a contract; if all are contracts, returns true; otherwise false
    :return: is_contract: bool
    """
    global CONTRACT_CACHE

    if addresses is None:
        return True

    if CONTRACT_CACHE.get(addresses) is not None:
        return CONTRACT_CACHE[addresses]
    else:
        is_contract = True
        for address in addresses.split(','):
            try:
                code = w3.eth.get_code(Web3.toChecksumAddress(address))
            except Exception as e:
                logging.error(f"Exception in is_contract {e}")

            is_contract = is_contract & (code != HexBytes('0x'))
        CONTRACT_CACHE[addresses] = is_contract
        return is_contract


def is_address(w3, addresses: str) -> bool:
    """
    this function determines whether address is a valid address
    :return: is_address: bool
    """
    if addresses is None:
        return True

    is_address = True
    for address in addresses.split(','):
        for c in ['a', 'b', 'c', 'd', 'e', 'f', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9']:
            test_str = c + c + c + c + c + c + c + c + c  # make a string of length 9; I know this is ugly, but regex didnt work
            if test_str in address.lower():
                is_address = False

    return is_address


def get_victim_info(alert_data: pd.DataFrame, victims: dict):
    victim_address, victim_name = "", ""
    victim_metadata = dict()

    df_intersection = alert_data[alert_data["transaction_hash"].isin(victims.keys())]

    if(len(df_intersection) > 0):
        tx_hash_with_victim_info = df_intersection.iloc[0]["transaction_hash"]
        victim_metadata = victims[tx_hash_with_victim_info]
        victim_address = victim_metadata["address1"] if "address1" in victim_metadata.keys() else ""
        victim_name = victim_metadata["tag1"] if "tag1" in victim_metadata.keys() else ""

    return victim_address, victim_name, victim_metadata




def detect_attack(w3, luabase: Luabase, alert_event: forta_agent.alert_event.AlertEvent):
    """
    this function returns finding for any address with at least 3 alerts observed on that address; it will generate an anomaly score
    :return: findings: list
    """
    global ALERT_ID_STAGE_MAPPING
    global ALERT_ID_AD_SCORER_MAPPING

    global ALERTED_CLUSTERS_LOOSE
    global ALERTED_CLUSTERS_STRICT
    global ALERT_DATA
    global FP_MITIGATION_CLUSTERS
    global VICTIMS
    global ENTITY_CLUSTERS
    global MUTEX
    global CHAIN_ID

    if int(alert_event.alert.source.block.chain_id) == CHAIN_ID:
        logging.info(f"alert {alert_event.alert_hash} received for propery chain {alert_event.chain_id}")

        #  assess whether we generate a finding
        #  note, only one instance will be running at a time to keep up with alert volume
        if not MUTEX:
            try:
                MUTEX = True

                # update entity clusters
                if in_list(alert_event, [(ENTITY_CLUSTER_BOT, ENTITY_CLUSTER_BOT_ALERT_ID)]):
                    logging.info(f"alert {alert_event.alert_hash} is entity cluster alert")
                    cluster = alert_event.alert.metadata["entityAddresses"].lower()

                    for address in cluster.split(','):
                        ENTITY_CLUSTERS[address] = cluster
                        logging.info(f"adding cluster mapping: {address} -> {cluster}")
                        while len(ENTITY_CLUSTERS) > ENTITY_CLUSTERS_MAX_QUEUE_SIZE:
                            ENTITY_CLUSTERS.pop(next(iter(ENTITY_CLUSTERS)))
                        logging.info(f"alert {alert_event.alert_hash} entity clusters size now: {len(ENTITY_CLUSTERS)}")

                        if ALERT_DATA.get(address) is not None:
                            alert_data = ALERT_DATA.pop(address)
                            if ALERT_DATA.get(cluster) is not None:
                                alert_data = pd.concat([alert_data, ALERT_DATA[cluster]], ignore_index=True, axis=0)
                            ALERT_DATA[cluster] = alert_data
                            logging.info(f"alert {alert_event.alert_hash} alert data size now: {len(ALERT_DATA)}")

                        if address in FP_MITIGATION_CLUSTERS:
                            FP_MITIGATION_CLUSTERS.append(cluster)
                            logging.info(f"alert {alert_event.alert_hash} FP mitigation clusters size now: {len(FP_MITIGATION_CLUSTERS)}")

                # update victim alerts
                if (in_list(alert_event, [(VICTIM_IDENTIFICATION_BOT, VICTIM_IDENTIFICATION_BOT_ALERT_IDS[0]),(VICTIM_IDENTIFICATION_BOT, VICTIM_IDENTIFICATION_BOT_ALERT_IDS[1])])):
                    logging.info(f"alert {alert_event.alert_hash} is a victim identification alert")
                    logging.info(f"alert {alert_event.alert_hash} adding victim identification list: Victim Identification list size now: {len(VICTIMS)}")
                    VICTIMS[alert_event.alert.source.transaction_hash] = alert_event.alert.metadata

                    while len(VICTIMS) > VICTIM_QUEUE_MAX_SIZE:
                        VICTIMS.pop(next(iter(VICTIMS)))


                # update FP mitigation clusters
                if in_list(alert_event, FP_MITIGATION_BOTS):
                    logging.info(f"alert {alert_event.alert_hash} is a FP mitigation alert")
                    address = alert_event.alert.description[0:42]
                    cluster = address
                    if address in ENTITY_CLUSTERS.keys():
                        cluster = ENTITY_CLUSTERS[address]
                    logging.info(f"alert {alert_event.alert_hash} adding FP mitigation cluster: {cluster}. FP mitigation clusters size now: {len(FP_MITIGATION_CLUSTERS)}")
                    update_list(FP_MITIGATION_CLUSTERS, FP_CLUSTERS_QUEUE_MAX_SIZE, cluster)

                # update alerts and process them for a given cluster
                if in_list(alert_event, BASE_BOTS):
                    logging.info(f"alert {alert_event.alert_hash}: is a base bot {alert_event.alert.source.bot.id}, {alert_event.alert_id} alert for addresses {alert_event.alert.addresses}")
                    try:
                        end_date = datetime.fromtimestamp(w3.eth.get_block(alert_event.block_number).timestamp)
                    except Exception as e:
                        logging.warn(f"Unable to get block for block number {alert_event.block_number} .{e}")
                        end_date = datetime.now()
                    start_date = end_date - timedelta(hours=ALERTS_LOOKBACK_WINDOW_IN_HOURS)

                    if LOCAL_NODE == 1:  # for local testing
                        logging.info(f"Populating denominator cache for {alert_event.chain_id} {ALERT_ID_AD_SCORER_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)]} {start_date} {end_date}")
                        luabase.populate_denominator_cache(chain_id=alert_event.chain_id, ad_scorer=ALERT_ID_AD_SCORER_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)], start_date=start_date, end_date=end_date)
                        logging.info(f"Populated denominator cache for {alert_event.chain_id} {ALERT_ID_AD_SCORER_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)]} {start_date} {end_date}")
                        logging.info(f"Populating alert count cache for {alert_event.chain_id} {alert_event.bot_id}, {alert_event.alert.alert_id}  {start_date} {end_date}")
                        luabase.populate_alert_count_cache(chain_id=alert_event.chain_id, bot_id=alert_event.alert.source.bot.id, alert_id=alert_event.alert_id, start_date=start_date, end_date=end_date)
                        logging.info(f"Populated alert count cache for {alert_event.chain_id} {alert_event.bot_id}, {alert_event.alert.alert_id}  {start_date} {end_date}")
                        
                    # add anomaly score and metadata to ALERT_DATA
                    for address in alert_event.alert.addresses:
                        logging.info(f"Analysing address {address}")
                        address_lower = address.lower()
                        cluster = address_lower
                        if address_lower in ENTITY_CLUSTERS.keys():
                            cluster = ENTITY_CLUSTERS[address_lower]
                        if(is_contract(w3, cluster) or not is_address(w3, cluster)):  # ignore contracts and invalid addresses like 0x0000000000000blabla
                            logging.info(f"alert {alert_event.alert_hash}: {cluster} is contract or not an address. Continue ... ")
                            continue

                        logging.info(f"alert {alert_event.alert_hash}: {cluster} is valid EOA.")

                        try:
                            alert_count = luabase.get_alert_count(alert_event.chain_id, alert_event.bot_id, alert_event.alert.alert_id, start_date, end_date)
                        except Exception as e:
                            logging.warn(f"Exception in get_alert_count {e}")
                            continue
                        ad_scorer = ALERT_ID_AD_SCORER_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)]
                        anomaly_score = AD_SCORE_ANOMALY_SCORE
                        if ad_scorer != 'ad-score':
                            try:
                                denominator = luabase.get_denominator(alert_event.chain_id, ALERT_ID_AD_SCORER_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)], start_date, end_date)
                            except Exception as e:
                                logging.warn(f"Exception in get_denominator {e}")
                                continue
                            anomaly_score = alert_count * 1.0 / denominator
                        stage = ALERT_ID_STAGE_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)]
                        logging.info(f"alert {alert_event.alert_hash} {alert_event.bot_id} {alert_event.alert.alert_id} {stage}: {cluster} anomaly score of {anomaly_score}")

                        if ALERT_DATA.get(cluster) is None:
                            ALERT_DATA[cluster] = pd.DataFrame(columns=['stage', 'created_at', 'anomaly_score', 'alert_hash', 'bot_id', 'alert_id', 'addresses', 'transaction_hash'])

                        alert_data = ALERT_DATA[cluster]
                        stage = ALERT_ID_STAGE_MAPPING[(alert_event.bot_id, alert_event.alert.alert_id)]
                        alert_data = pd.concat([alert_data, pd.DataFrame([[stage, datetime.strptime(alert_event.alert.created_at[:-4] + 'Z', "%Y-%m-%dT%H:%M:%S.%fZ"), anomaly_score, alert_event.alert_hash, alert_event.bot_id, alert_event.alert.alert_id, alert_event.alert.addresses, alert_event.alert.source.transaction_hash]], columns=['stage', 'created_at', 'anomaly_score', 'alert_hash', 'bot_id', 'alert_id', 'addresses', 'transaction_hash'])], ignore_index=True, axis=0)
                        logging.info(f"alert data size now: {len(alert_data)}")

                        # add new alert and purge old alerts
                        ALERT_DATA[cluster] = alert_data[alert_data['created_at'] > start_date]
                        alert_data = ALERT_DATA[cluster]
                        logging.info(f"alert data size now: {len(ALERT_DATA)}")

                        # analyze ALERT_DATA to see whether conditions are met to generate a finding
                        # 1. Have to have at least MIN_ALERTS_COUNT bots reporting alerts
                        if len(alert_data['bot_id'].drop_duplicates(inplace=False)) >= MIN_ALERTS_COUNT:
                            # 2. Have to have overall anomaly score of less than ANOMALY_SCORE_THRESHOLD
                            anomaly_scores = alert_data[['stage', 'anomaly_score']].drop_duplicates(inplace=False)
                            anomaly_scores = anomaly_scores.groupby('stage').min()
                            anomaly_score = anomaly_scores['anomaly_score'].prod()
                            logging.info(f"Have sufficient number of alerts for {cluster}. Overall anomaly score is {anomaly_score}, {len(anomaly_scores)} stages.")
                            logging.info(f"{cluster} anomaly scores {anomaly_scores}.")

                            if cluster in FP_MITIGATION_CLUSTERS:
                                logging.info(f"Mitigating FP for {cluster}. Wont raise finding")
                            if anomaly_score >= ANOMALY_SCORE_THRESHOLD_LOOSE and len(anomaly_scores) < 4:
                                logging.info(f"Overall anomaly score for {cluster} is above threshold and not 4 stages have been observed. Wont raise finding")

                            if (anomaly_score < ANOMALY_SCORE_THRESHOLD_STRICT or len(anomaly_scores) == 4) and cluster not in FP_MITIGATION_CLUSTERS and cluster not in ALERTED_CLUSTERS_STRICT:
                                victim_address, victim_name, victim_metadata = get_victim_info(alert_data, VICTIMS)
                                update_list(ALERTED_CLUSTERS_STRICT, ALERTED_CLUSTERS_MAX_QUEUE_SIZE, cluster)
                                FINDINGS_CACHE.append(AlertCombinerFinding.create_finding(cluster, victim_address, victim_name, anomaly_score, FindingSeverity.Critical, "ATTACK-DETECTOR-1", alert_event, alert_data, victim_metadata))
                            elif anomaly_score < ANOMALY_SCORE_THRESHOLD_LOOSE and cluster not in FP_MITIGATION_CLUSTERS and cluster not in ALERTED_CLUSTERS_LOOSE and cluster not in ALERTED_CLUSTERS_STRICT:
                                victim_address, victim_name, victim_metadata = get_victim_info(alert_data, VICTIMS)
                                update_list(ALERTED_CLUSTERS_LOOSE, ALERTED_CLUSTERS_MAX_QUEUE_SIZE, cluster)
                                FINDINGS_CACHE.append(AlertCombinerFinding.create_finding(cluster,  victim_address, victim_name, anomaly_score, FindingSeverity.Low, "ATTACK-DETECTOR-2", alert_event, alert_data, victim_metadata))

                MUTEX = False
                logging.info("Set MUTEX to False")
            except Exception as e:
                logging.warn(f"Exception in process_alert {e}")
                MUTEX = False
                logging.info("Set MUTEX to False")

    else:
        logging.debug(f"alert {alert_event.alert_hash} received for incorrect chain {alert_event.chain_id}. This bot is for chain {CHAIN_ID}.")


def update_list(items: list, max_size: int, item: str):

    items.append(item.lower())

    while len(items) > max_size:
        items.pop(0)  # remove oldest item


def in_list(alert_event: forta_agent.alert_event.AlertEvent, bots: tuple) -> bool:
    """
    this function returns True if the alert is from a bot in the bots tuple
    :return: bool
    """
    for tup in bots:
        if alert_event.alert.source.bot.id == tup[0] and alert_event.alert.alert_id == tup[1]:
            return True

    return False


def persist_state():
    global ALERTS_DATA_KEY
    global FP_MITIGATION_CLUSTERS_KEY
    global ALERTED_CLUSTERS_STRICT_KEY
    global ALERTED_CLUSTERS_LOOSE_KEY
    global ENTITY_CLUSTERS_KEY
    global CHAIN_ID

    persist(ALERT_DATA, CHAIN_ID, ALERTS_DATA_KEY)
    persist(FP_MITIGATION_CLUSTERS, CHAIN_ID, FP_MITIGATION_CLUSTERS_KEY)
    persist(ENTITY_CLUSTERS, CHAIN_ID, ENTITY_CLUSTERS_KEY)
    persist(ALERTED_CLUSTERS_LOOSE, CHAIN_ID, ALERTED_CLUSTERS_LOOSE_KEY)
    persist(ALERTED_CLUSTERS_STRICT, CHAIN_ID, ALERTED_CLUSTERS_STRICT_KEY)
    logging.info("Persisted bot state.")


def persist(obj: object, chain_id: int, key: str):
    L2Cache.write(obj, chain_id, key)


def load(chain_id: int, key: str) -> object:
    return L2Cache.load(chain_id, key)


def provide_handle_alert(w3, luabase):
    logging.debug("provide_handle_alert called")

    def handle_alert(alert_event: forta_agent.alert_event.AlertEvent) -> list:
        logging.debug("handle_alert inner called")

        global FINDINGS_CACHE
        global MUTEX
        global MUTEX_LUABASE

        #  detect_attack(w3, luabase, alert_event)
        if not MUTEX:
            if LOCAL_NODE == 1:
                detect_attack(w3, luabase, alert_event)
                persist_state()
            else:
                thread = threading.Thread(target=detect_attack, args=(w3, luabase, alert_event))
                thread.start()
        else:
            logging.debug("Detect_attack not called. Mutex is locked")

        end_date = datetime.now()
        start_date = end_date - timedelta(hours=ALERTS_LOOKBACK_WINDOW_IN_HOURS)
        #  luabase.populate_cache(CHAIN_ID, start_date, end_date)
        if not MUTEX_LUABASE:
            if LOCAL_NODE == 0:
                thread = threading.Thread(target=luabase.populate_cache, args=(CHAIN_ID, start_date, end_date))
                thread.start()
        else:
            logging.debug("Populate_cache not called. Mutex is locked")

        #  uncomment for local testing of tx/block ranges (ok for npm run start); otherwise the process will exit
        #  while (thread.is_alive()):
        #    pass
        findings = FINDINGS_CACHE
        FINDINGS_CACHE = []
        return findings

    return handle_alert


real_handle_alert = provide_handle_alert(web3, luabase)


def handle_alert(alert_event: forta_agent.alert_event.AlertEvent) -> list:
    logging.debug("handle_alert called")
    return real_handle_alert(alert_event)


def handle_block(block_event: forta_agent.BlockEvent):
    logging.debug("handle_block called")

    if datetime.now().minute == 0:  # every hour
        persist_state()

    return []
