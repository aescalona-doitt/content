import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *
import requests
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
requests.packages.urllib3.disable_warnings()

if not demisto.params()['proxy']:
    del os.environ['HTTP_PROXY']
    del os.environ['HTTPS_PROXY']
    del os.environ['http_proxy']
    del os.environ['https_proxy']

''' GLOBAL VARS '''

SERVER = demisto.params()['url'][:-1] if demisto.params()['url'].endswith('/') else demisto.params()['url']
BASE_URL = SERVER + '/api'
TENANT_ID = demisto.params()['tenant_id']
AUTH_AND_TOKEN_URL = demisto.params()['auth_id'].split('@')
AUTH_ID = AUTH_AND_TOKEN_URL[0]
ENC_KEY = demisto.params()['enc_key']
USE_SSL = not demisto.params().get('insecure', False)
FETCH_SEVERITY = demisto.params()['fetch_severity'].split(',')
FETCH_STATUS = demisto.params().get('fetch_status').split(',')
if len(AUTH_AND_TOKEN_URL) != 2:
    TOKEN_RETRIEVAL_URL = 'https://oproxy.demisto.ninja/obtain-token'  # disable-secrets-detection
else:
    TOKEN_RETRIEVAL_URL = AUTH_AND_TOKEN_URL[1]
APP_NAME = 'ms-defender-atp'

''' HELPER FUNCTIONS '''


def epoch_seconds(d=None):
    """
    Return the number of seconds for given date. If no date, return current.
    """
    if not d:
        d = datetime.utcnow()
    return int((d - datetime.utcfromtimestamp(0)).total_seconds())


def get_encrypted(content: str, key: str) -> str:
    """

    Args:
        content (str): content to encrypt. For a request to Demistobot for a new access token, content should be
            the tenant id
        key (str): encryption key from Demistobot

    Returns:
        encrypted timestamp:content
    """
    def create_nonce() -> bytes:
        return os.urandom(12)

    def encrypt(string: str, enc_key: str) -> bytes:
        """

        Args:
            enc_key (str):
            string (str):

        Returns:
            bytes:
        """
        # String to bytes
        enc_key = base64.b64decode(enc_key)
        # Create key
        aes_gcm = AESGCM(enc_key)
        # Create nonce
        nonce = create_nonce()
        # Create ciphered data
        data = string.encode()
        ct = aes_gcm.encrypt(nonce, data, None)
        return base64.b64encode(nonce + ct)
    now = epoch_seconds()
    encrypted = encrypt(f'{now}:{content}', key).decode('utf-8')
    return encrypted


def get_access_token():
    integration_context = demisto.getIntegrationContext()
    access_token = integration_context.get('access_token')
    valid_until = integration_context.get('valid_until')
    calling_context = demisto.callingContext.get('context', {})  # type: ignore[attr-defined]
    brand_name = calling_context.get('IntegrationBrand', '')
    instance_name = calling_context.get('IntegrationInstance', '')
    if access_token and valid_until:
        if epoch_seconds() < valid_until:
            return access_token
    headers = {'Accept': 'application/json'}
    headers['X-Content-Version'] = CONTENT_RELEASE_VERSION
    headers['X-Branch-Name'] = CONTENT_BRANCH_NAME
    headers['X-Content-Name'] = brand_name or instance_name or 'Name not found'

    dbot_response = requests.post(
        TOKEN_RETRIEVAL_URL,
        headers=headers,
        data=json.dumps({
            'app_name': APP_NAME,
            'registration_id': AUTH_ID,
            'encrypted_token': get_encrypted(TENANT_ID, ENC_KEY)
        }),
        verify=USE_SSL
    )
    if dbot_response.status_code not in {200, 201}:
        msg = 'Error in authentication. Try checking the credentials you entered.'
        try:
            demisto.info('Authentication failure from server: {} {} {}'.format(
                dbot_response.status_code, dbot_response.reason, dbot_response.text))
            err_response = dbot_response.json()
            server_msg = err_response.get('message')
            if not server_msg:
                title = err_response.get('title')
                detail = err_response.get('detail')
                if title:
                    server_msg = f'{title}. {detail}'
            if server_msg:
                msg += ' Server message: {}'.format(server_msg)
        except Exception as ex:
            demisto.error('Failed parsing error response - Exception: {}'.format(ex))
        raise Exception(msg)
    try:
        gcloud_function_exec_id = dbot_response.headers.get('Function-Execution-Id')
        demisto.info(f'Google Cloud Function Execution ID: {gcloud_function_exec_id}')
        parsed_response = dbot_response.json()
    except ValueError:
        raise Exception(
            'There was a problem in retrieving an updated access token.\n'
            'The response from the Demistobot server did not contain the expected content.'
        )
    access_token = parsed_response.get('access_token')
    expires_in = parsed_response.get('expires_in', 3595)
    time_now = epoch_seconds()
    time_buffer = 5  # seconds by which to shorten the validity period
    if expires_in - time_buffer > 0:
        # err on the side of caution with a slightly shorter access token validity period
        expires_in = expires_in - time_buffer

    demisto.setIntegrationContext({
        'access_token': access_token,
        'valid_until': time_now + expires_in
    })
    return access_token


def http_request(method, url_suffix, json=None, data=None, params=None):

    token = get_access_token()
    r = requests.request(
        method,
        BASE_URL + url_suffix,
        json=json,
        headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json'
        },
        verify=USE_SSL,
        data=data

    )
    if r.status_code not in {200, 201}:
        try:
            error = r.json().get('error')
            msg = error['message'] if 'message' in error else r.reason
            return_error('Error in API call to ATP [%d] - %s' % (r.status_code, r.text))
        except ValueError:
            msg = r.text if r.text else r.reason
            return_error('Error in API call to ATP [%d] - %s' % (r.status_code, msg))
    if not r.text:
        return {}
    try:
        return r.json()
    except ValueError:
        return {}


def alert_to_incident(alert):
    incident = {}
    incident['rawJSON'] = json.dumps(alert)
    incident['name'] = 'Windows Defender ATP Alert ' + alert['id']
    return incident


def capitalize_first_letter(string):
    return string[:1].upper() + string[1:]


''' FUNCTIONS '''


def isolate_machine_command():

    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    isolation_type = demisto.args().get('isolation_type')
    response = isolate_machine(machine_id, comment, isolation_type)
    ec = {
        'MicrosoftATP.Machine(val.ID && val.ID === obj.ID)': {
            'ID': machine_id,
            'Isolation': {
                'Isolated': True,
                'Requestor': response.get('requestor'),
                'RequestorComment': response.get('requestorComment')
            }
        }
    }

    entry = {
        'Type': entryTypes['note'],
        'Contents': response,
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': 'The isolation request has been submitted successfully',
        'EntryContext': ec
    }
    demisto.results(entry)


def isolate_machine(machine_id, comment, isolation_type):

    cmd_url = '/machines/{}/isolate'.format(machine_id)
    json = {
        "Comment": comment,
        "IsolationType": isolation_type
    }
    response = http_request('POST', cmd_url, json=json)
    return response


def unisolate_machine_command():

    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    response = unisolate_machine(machine_id, comment)
    ec = {
        'MicrosoftATP.Machine(val.ID && val.ID === obj.ID)': {
            'ID': machine_id,
            'Isolation': {
                'Isolated': False,
                'Requestor': response.get('requestor'),
                'RequestorComment': response.get('requestorComment')
            }
        }
    }

    entry = {
        'Type': entryTypes['note'],
        'Contents': response,
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': 'The request to stop the isolation has been submitted successfully',
        'EntryContext': ec
    }
    demisto.results(entry)


def unisolate_machine(machine_id, comment):

    cmd_url = '/machines/{}/unisolate'.format(machine_id)
    json = {
        'Comment': comment
    }
    response = http_request('POST', cmd_url, json=json)
    return response


def get_machines_command():

    machines = get_machines().get('value', [])

    hostname = demisto.args().get('hostname')
    ip = demisto.args().get('ip')
    risk_score = demisto.args().get('risk_score')
    health_status = demisto.args().get('health_status')

    output = []
    endpoint_context = []

    for machine in machines:
        computer_dns_name = machine.get('computerDnsName')
        last_external_ip = machine.get('lastExternalIpAddress')
        machine_risk_score = machine.get('riskScore')
        machine_health_status = machine.get('healthStatus')
        if (hostname and hostname != computer_dns_name) or (ip and ip != last_external_ip) or \
                (risk_score and risk_score != machine_risk_score) or \
                (health_status and health_status != machine_health_status):
            continue
        current_machine_output = {
            'ComputerDNSName': computer_dns_name,
            'ID': machine.get('id'),
            'AgentVersion': machine.get('agentVersion'),
            'FirstSeen': machine.get('firstSeen'),
            'LastSeen': machine.get('lastSeen'),
            'HealthStatus': machine_health_status,
            'IsAADJoined': machine.get('isAadJoined'),
            'LastExternalIPAddress': last_external_ip,
            'LastIPAddress': machine.get('lastIpAddress'),
            'Tags': machine.get('machineTags'),
            'OSBuild': machine.get('osBuild'),
            'OSPlatform': machine.get('osPlatform'),
            'RBACGroupID': machine.get('rbacGroupId'),
            'RiskScore': machine_risk_score
        }
        current_endpoint_output = {
            'Hostname': machine.get('computerDnsName'),
            'IPAddress': machine.get('lastExternalIpAddress'),
            'OS': machine.get('osPlatform')
        }
        rbac_group_name = machine.get('rbacGroupName')
        if rbac_group_name:
            current_machine_output['RBACGroupName'] = rbac_group_name
        aad_device_id = machine.get('aadDeviceId')
        if aad_device_id:
            current_machine_output['AADDeviceID'] = aad_device_id
        os_version = machine.get('osVersion')
        if os_version:
            current_machine_output['OSVersion'] = os_version
            current_endpoint_output['OSVersion'] = os_version
        output.append(current_machine_output)
        endpoint_context.append(current_endpoint_output)

    if output:
        ec = {
            'MicrosoftATP.Machine(val.ID && val.ID === obj.ID)': output,
            'Endpoint(val.Hostname && val.Hostname === obj.Hostname)': endpoint_context
        }

        entry = {
            'Type': entryTypes['note'],
            'Contents': machines,
            'ContentsFormat': formats['json'],
            'ReadableContentsFormat': formats['markdown'],
            'HumanReadable': tableToMarkdown('Windows Defender ATP machines', output, removeNull=True),
            'EntryContext': ec
        }
    else:
        entry = 'No results found'  # type: ignore
    demisto.results(entry)


def get_machines():

    cmd_url = '/machines'
    response = http_request('GET', cmd_url)
    return response


def get_file_related_machines_command():
    file = demisto.args()['file']
    machines = get_file_related_machines(file).get('value', [])
    if machines:
        output = []
        endpoint_context = []
        for machine in machines:
            current_machine_output = get_machine_data(machine.get('id'))
            current_machine_output['RelatedFile'] = file
            current_endpoint_output = {
                'Hostname': machine.get('computerDnsName'),
                'IPAddress': machine.get('lastExternalIpAddress'),
                'OS': machine.get('osPlatform')
            }
            os_version = machine.get('osVersion')
            if os_version:
                current_endpoint_output['OSVersion'] = os_version
            output.append(current_machine_output)
            endpoint_context.append(current_endpoint_output)

        ec = {
            'MicrosoftATP.Machine(val.ID && val.ID === obj.ID)': output,
            'Endpoint(val.Hostname && val.Hostname === obj.Hostname)': endpoint_context
        }

        title = 'Windows Defender ATP machines related to file {}'.format(file)
        entry = {
            'Type': entryTypes['note'],
            'Contents': machines,
            'ContentsFormat': formats['json'],
            'ReadableContentsFormat': formats['markdown'],
            'HumanReadable': tableToMarkdown(title, output, removeNull=True),
            'EntryContext': ec
        }
    else:
        entry = 'No results found'  # type: ignore
    demisto.results(entry)


def get_file_related_machines(file):

    cmd_url = '/files/{}/machines'.format(file)
    response = http_request('GET', cmd_url)
    return response


def get_machine_details_command():

    machine_id = demisto.args()['machine_id']
    machine = get_machine_details_request(machine_id)
    if machine:
        output = []
        endpoint_context = []
        current_machine_output = get_machine_data(machine_id)
        current_endpoint_output = {
            'Hostname': machine.get('computerDnsName'),
            'IPAddress': machine.get('lastExternalIpAddress'),
            'OS': machine.get('osPlatform')
        }
        os_version = machine.get('osVersion')
        if os_version:
            current_endpoint_output['OSVersion'] = os_version
        output.append(current_machine_output)
        endpoint_context.append(current_endpoint_output)
        ec = {
            'MicrosoftATP.Machine(val.ID && val.ID === obj.ID)': output,
            'Endpoint(val.Hostname && val.Hostname === obj.Hostname)': endpoint_context
        }

        title = 'Windows Defender ATP machine {} details'.format(machine_id)
        entry = {
            'Type': entryTypes['note'],
            'Contents': machine,
            'ContentsFormat': formats['json'],
            'ReadableContentsFormat': formats['markdown'],
            'HumanReadable': tableToMarkdown(title, output, removeNull=True),
            'EntryContext': ec
        }
    else:
        entry = 'No results found'  # type: ignore
    demisto.results(entry)


def get_machine_details_request(machine_id):

    cmd_url = '/machines/{}'.format(machine_id)
    response = http_request('GET', cmd_url)
    return response


def block_file_command():

    file_sha1 = demisto.args().get('sha1')
    comment = demisto.args().get('comment')
    title = demisto.args().get('title')
    expiration_time = demisto.args().get('expiration_time')
    severity = demisto.args().get('severity')
    recommended_actions = demisto.args().get('recommended_actions')

    block_file(file_sha1, comment, title, expiration_time, severity, recommended_actions)


def block_file(file_sha1, comment, title, expiration_time, severity, recommended_actions):

    cmd_url = '/tiindicators'
    json = {
        'indicator': file_sha1,
        'indicatorType': 'FileSha1',
        'action': 'AlertAndBlock',
        'title': title,
        'expirationTime': expiration_time,
        'severity': severity,
        'description': comment,
        'recommendedActions': recommended_actions
    }
    response = http_request('POST', cmd_url, json=json)
    return response


def get_user_related_machines(user_id):

    cmd_url = '/users/{}/machines'.format(user_id)
    response = http_request('GET', cmd_url)
    return response


def run_antivirus_scan_command():

    machine_id = demisto.args().get('machine_id')
    scan_type = demisto.args().get('scan_type')
    comment = demisto.args().get('comment')

    run_antivirus_scan(machine_id, comment, scan_type)

    demisto.results('Antivirus scan successfully triggered')


def run_antivirus_scan(machine_id, comment, scan_type):

    cmd_url = '/machines/{}/runAntiVirusScan'.format(machine_id)
    json = {
        'Comment': comment,
        'ScanType': scan_type
    }
    response = http_request('POST', cmd_url, json=json)
    return response


def list_alerts_command():

    alerts = list_alerts().get('value', [])

    severity = demisto.args().get('severity')
    status = demisto.args().get('status')

    output = []
    for alert in alerts:
        alert_severity = alert.get('severity')
        alert_status = alert.get('status')
        if (severity and severity != alert_severity) or (status and status != alert_status):
            continue
        current_alert_output = {}
        for key, value in alert.items():
            if value or value is False:
                current_alert_output[capitalize_first_letter(key).replace('Id', 'ID')] = value
        output.append(current_alert_output)

    if output:
        ec = {
            'MicrosoftATP.Alert(val.ID && val.ID === obj.ID)': output
        }

        title = 'Windows Defender ATP alerts'

        entry = {
            'Type': entryTypes['note'],
            'Contents': alerts,
            'ContentsFormat': formats['json'],
            'ReadableContentsFormat': formats['markdown'],
            'HumanReadable': tableToMarkdown(title, output, removeNull=True),
            'EntryContext': ec
        }
    else:
        entry = 'No results found'  # type: ignore
    demisto.results(entry)


def list_alerts():
    cmd_url = '/alerts'
    response = http_request('GET', cmd_url)
    return response


def update_alert_command():

    alert_id = demisto.args()['alert_id']
    assigned_to = demisto.args().get('assigned_to')
    status = demisto.args().get('status')
    classification = demisto.args().get('classification')
    determination = demisto.args().get('determination')

    if all(v is None for v in [assigned_to, status, classification, determination]):
        return_error('No arguments were given to update the alert')

    json = {}
    context = {
        'ID': alert_id
    }
    if assigned_to:
        json['assignedTo'] = assigned_to
        context['AssignedTo'] = assigned_to
    if status:
        json['status'] = status
        context['Status'] = status
    if classification:
        json['classification'] = classification
        context['Classification'] = classification
    if determination:
        json['determination'] = determination
        context['Determination'] = determination

    update_alert(alert_id, json)

    ec = {
        'MicrosoftATP.Alert(val.ID && val.ID === obj.ID)': context
    }

    entry = {
        'Type': entryTypes['note'],
        'Contents': '',
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': 'Alert {0} was updated successfully'.format(alert_id),
        'EntryContext': ec
    }

    demisto.results(entry)


def update_alert(alert_id, json):
    cmd_url = '/alerts/' + alert_id
    response = http_request('PATCH', cmd_url, json=json)
    return response


def get_alert_related_domains_request(alert_id):
    """Retrieves all domains related to a specific alert.
    Args:
        alert_id (str): Alert ID
    Returns:
        dict. Related domains
    """
    cmd_url = '/alerts/{}/domains'.format(alert_id)
    response = http_request('GET', cmd_url)
    return response


def get_alert_related_files_request(alert_id):
    """Retrieves all files related to a specific alert.
    Args:
        alert_id (str): Alert ID
    Returns:
        dict. Related files
    """
    cmd_url = '/alerts/{}/files'.format(alert_id)
    response = http_request('GET', cmd_url)
    return response


def get_alert_related_ips_request(alert_id):
    """Retrieves all IPs related to a specific alert.
    Args:
        alert_id (str): Alert ID
    Returns:
        dict. Related IPs
    """
    cmd_url = '/alerts/{}/ips'.format(alert_id)
    response = http_request('GET', cmd_url)
    return response


def get_alert_related_user_request(alert_id):
    """Retrieves the User related to a specific alert.
    Args:
        alert_id (str): Alert ID
    Returns:
        dict. Related user
    """
    cmd_url = '/alerts/{}/user'.format(alert_id)
    response = http_request('GET', cmd_url)
    return response


def get_machine_action_by_id_request(action_id):
    """Retrieves specific Machine Action by its ID.
    Args:
        action_id (str): Action ID
    Returns:
        dict. Machine Action entity
    """
    cmd_url = '/machineactions/{}'.format(action_id)
    response = http_request('GET', cmd_url)
    return response


def get_machine_actions_request():
    """Retrieves all Machine Action
    Returns:
        dict. Machine Action entity
    """
    cmd_url = '/machineactions'
    response = http_request('GET', cmd_url)
    return response

def get_investigation_package_request(machine_id, comment):
    """Collect investigation package from a machine.
    Args:
        machine_id (str): Machine ID
        comment (str): Comment to associate with the action
    Returns:
        dict. Machine's investigation_package
    """
    cmd_url = '/machines/{}/collectInvestigationPackage'.format(machine_id)
    json = {
        'Comment': comment
        }
    response = http_request('POST', cmd_url, json=json)
    return response

def get_investigation_package_sas_uri_request(action_id):
    """Get a URI that allows downloading of an Investigation package.
    Args:
        action_id (str): Action ID
    Returns:
        dict. An object that holds the link for the package
    """
    cmd_url = '/machineactions/{}/getPackageUri'.format(action_id)
    response = http_request('GET', cmd_url)
    return response

def restrict_app_execution_request(machine_id, comment):
    """Restrict execution of all applications on the machine except a predefined set.
    Args:
        machine_id (str): Machine ID
        comment (str): Comment to associate with the action
    Returns:
        dict. Machine action
    """
    cmd_url = '/machines/{}/restrictCodeExecution'.format(machine_id)
    json = {
        'Comment': comment
        }
    response = http_request('POST', cmd_url, json=json)
    return response

def remove_app_restriction_request(machine_id, comment):
    """Enable execution of any application on the machine.
    Args:
        machine_id (str): Machine ID
        comment (str): Comment to associate with the action
    Returns:
        dict. Machine action
    """
    cmd_url = '/machines/{}/unrestrictCodeExecution'.format(machine_id)
    json = {
        'Comment': comment
        }
    response = http_request('POST', cmd_url, json=json)
    return response


def stop_and_quarantine_file_request(machine_id, file_sha1, comment):
    """Stop execution of a file on a machine and delete it.
    Args:
        machine_id (str): Machine ID
        file_sha1: (str): File's hash
        comment (str): Comment to associate with the action
    Returns:
        dict. Machine action
    """
    cmd_url = '/machines/{}/stopAndQuarantineFile'.format(machine_id)
    json = {
        'Comment': comment,
        'Sha1': file_sha1
    }
    response = http_request('POST', cmd_url, json=json)
    return response

def get_investigation_by_id_request(investigation_id):
    """Get the investigation ID and return the investigation details
    Args:
        investigation_id (str): The investigation ID
    Returns:
        dict. Investigations entity
    """
    cmd_url = '/investigations/{}'.format(investigation_id)
    response = http_request('GET', cmd_url)
    return response

def get_alert_by_id_request(alert_id):
    """Get the alert ID and return the alert details
    Args:
        alert_id (str): The alert ID
    Returns:
        dict. Alert's entity
    """
    cmd_url = '/alerts/{}'.format(alert_id)
    response = http_request('GET', cmd_url)
    return response

def get_investigation_list_request():
    """Retrieves a collection of Investigations.
    Returns:
        dict. A collection of Investigations entities.
    """
    cmd_url = '/investigations'
    response = http_request('GET', cmd_url)
    return response

def start_investigation_request(machine_id, comment):
    """Start automated investigation on a machine.
    Args:
          machine_id (str): The Machine ID
          comment (str): Comment to associate with the action
    Returns:
        dict. Investigation's entity
    """
    cmd_url = '/machines/{}/startInvestigation'.format(machine_id)
    json = {
        'Comment': comment,
    }
    response = http_request('POST', cmd_url, json=json)
    return response

def get_domain_statistics_request(domain):
    """Retrieves the statistics on the given domain.
    Args:
        domain (str): The Domain's address
    Returns:
        dict. Domain's statistics
    """
    cmd_url = '/domains/{}/stats'.format(domain)
    response = http_request('GET', cmd_url)
    return response

def get_file_statistics_request(file_sha1):
    """Retrieves the statistics on the given file.
    Args:
        file_sha1 (str): The file's hash
    Returns:
        dict. File's statistics
    """
    cmd_url = '/files/{}/stats'.format(file_sha1)
    response = http_request('GET', cmd_url)
    return response

def get_ip_statistics_request(ip):
    """Retrieves the statistics on the given IP.
    Args:
        ip (str): The IP address
    Returns:
        dict. IP's statistics
    """
    cmd_url = '/ips/{}/stats'.format(ip)
    response = http_request('GET', cmd_url)
    return response

def get_domain_alerts_request(domain):
    """Retrieves a collection of Alerts related to a given domain address.
    Args:
        domain (str): The Domain's address
    Returns:
        dict. Alerts entities
    """
    cmd_url = '/domains/{}/alerts'.format(domain)
    response = http_request('GET', cmd_url)
    return response

def get_file_alerts_request(file_sha1):
    """Retrieves a collection of Alerts related to a given file hash.
    Args:
        file_sha1 (str): The file's hash
    Returns:
        dict. Alerts entities
    """
    cmd_url = '/files/{}/alerts'.format(file_sha1)
    response = http_request('GET', cmd_url)
    return response

def get_ip_alerts_request(ip):
    """Retrieves a collection of Alerts related to a given IP.
    Args:
        ip (str): The IP address
    Returns:
        dict. Alerts entities
    """
    cmd_url = '/ips/{}/alerts'.format(ip)
    response = http_request('GET', cmd_url)
    return response

def get_user_alerts_request(username):
    """Retrieves a collection of Alerts related to a given  user ID.
    Args:
        username (str): The user ID
    Returns:
        dict. Alerts entities
    """
    cmd_url = '/users/{}/alerts'.format(username)
    response = http_request('GET', cmd_url)
    return response

def get_domain_machines_request(domain):
    """Retrieves a collection of Machines that have communicated to or from a given domain address.
    Args:
        domain (str): The Domain's address
    Returns:
        dict. Machines entities
    """
    cmd_url = '/domains/{}/alerts'.format(domain)
    response = http_request('GET', cmd_url)
    return response

def get_user_machines_request(username):
    """Retrieves a collection of machines related to a given user ID.
    Args:
        username (str): The user ID
    Returns:
        dict. Machines entities
    """
    cmd_url = '/users/{}/machines'.format(username)
    response = http_request('GET', cmd_url)
    return response

def add_remove_machine_tag_request(machine_id, action, tag):
    """Retrieves a collection of machines related to a given user ID.
    Args:
        machine_id (str): The machine ID
        action (str): Add or Remove action
        tag (str): The tag name
    Returns:
        dict. Updated machine's entity
    """
    cmd_url = '/machines/{}/tags'.format(machine_id)
    new_tags = {
        "Value" : tag,
        "Action": action
    }
    response = http_request('POST', cmd_url, json=new_tags)
    return response

def get_file_data_request(file_hash):
    """Retrieves a File by identifier Sha1.
    Args:
        file_hash(str): The file sha1 hash
    Returns:
        dict. File entities
    """
    cmd_url = '/files/{}'.format(file_hash)
    response = http_request('GET', cmd_url)
    return response

def get_advanced_hunting_command():
    query = demisto.args().get('query')
    response = get_advanced_hunting(query)
    results = response.get('Results')
    if isinstance(results, list) and len(results) == 1:
        report_id = results[0].get('ReportId')
        if report_id:
            results[0]['ReportId'] = str(report_id)
    ec = {
        'MicrosoftATP.Hunt.Result': results
    }
    hr = tableToMarkdown('Hunt results', results, removeNull=True)

    entry = {
        'Type': entryTypes['note'],
        'Contents': response,
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': hr,
        'EntryContext': ec
    }

    demisto.results(entry)


def get_advanced_hunting(query):
    cmd_url = '/advancedqueries/run'
    json = {
        'Query': query
    }
    response = http_request('POST', cmd_url, json=json)
    return response


def create_alert_command():
    args = demisto.args()
    response = create_alert(
        args.get('machine_id'),
        args.get('severity'),
        args.get('title'),
        args.get('description'),
        args.get('event_time'),
        args.get('report_id'),
        args.get('recommended_action'),
        args.get('category')
    )
    output = {
        'MachineID': response.get('machineId'),
        'RecommendedAction': response.get('recommendedAction'),
        'Title': response.get('title'),
        'Description': response.get('description'),
        'Severity': response.get('severity'),
        'Category': response.get('Category'),
        'ReportID': response.get('reportId'),
        'ID': response.get('id'),
        'Status': response.get('status')
    }
    output = {k: v for k, v in output.items() if v is not None}
    ec = {
        'MicrosoftATP.Alert': output
    }
    hr = tableToMarkdown('Alert created:', output)

    entry = {
        'Type': entryTypes['note'],
        'Contents': response,
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': hr,
        'EntryContext': ec
    }

    demisto.results(entry)


def create_alert(machine_id, severity, title, description, event_time, report_id, rec_action, category):
    cmd_url = '/alerts/CreateAlertByReference'
    json = {
        'machineId': machine_id,
        'severity': severity,
        'title': title,
        'description': description,
        'eventTime': event_time,
        'reportId': report_id
    }
    if rec_action:
        json['recommendedAction'] = rec_action
    if category:
        json['category'] = category
    response = http_request('POST', cmd_url, json=json)
    return response


def get_alert_related_user_command():
    alert_id = demisto.args().get('id')
    response = get_alert_related_user_request(alert_id)
    output = {
        'ID': response.get('id'),
        'AlertID': alert_id,
        'FirstSeen': response.get('firstSeen'),
        'LastSeen': response.get('lastSeen'),
        'MostPrevalentMachineID': response.get('mostPrevalentMachineId'),
        'LogonTypes': response.get('logonTypes'),
        'LogonCount': response.get('logOnMachinesCount'),
        'DomainAdmin': response.get('isDomainAdmin'),
        'NetworkUser': response.get('isOnlyNetworkUser')
    }
    ec = {
        'MicrosoftATP.User(val.AlertID === obj.AlertID && val.ID === obj.ID)': output
    }
    hr = tableToMarkdown('Alert Related User:', output, removeNull=True)

    entry = {
        'Type': entryTypes['note'],
        'Contents': response,
        'ContentsFormat': formats['json'],
        'ReadableContentsFormat': formats['markdown'],
        'HumanReadable': hr,
        'EntryContext': ec
    }

    demisto.results(entry)


def get_alert_related_files_command():
    """Retrieves all files related to a specific alert.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    alert_id = demisto.args().get('id')
    limit = int(demisto.args().get('limit'))
    response_files_list = get_alert_related_files_request(alert_id)
    files_data_list = []
    headers = ['Sha1', 'Sha256', 'Md5', 'GlobalPrevalence', 'GlobalFirstObserved','GlobalLastObserved', 'Size',
               'FileType', 'IsPeFile', 'FilePublisher', 'FileProductName', 'Signer', 'Issuer', 'SignerHash',
               'IsValidCertificate', 'DeterminationType', 'DeterminationValue' ]
    for file_obj in response_files_list['value']:
        files_data_list.append(get_file_data(file_obj['sha1']))
        if len(files_data_list) == limit:
            break
    context_output = {
        'AlertID': alert_id,
        'Files': files_data_list
    }
    ec = {
        'MicrosoftATP.AlertFile(val.AlertID === obj.AlertID)': context_output
    }
    hr = tableToMarkdown('Alert {} Related Files:'.format(alert_id), files_data_list, headers=headers)
    return hr, ec, response_files_list


def get_file_data(file_sha1):
    """Get file's hash and returns the file's info
    Returns:
        dict. File's info
    """
    response = get_file_data_request(file_sha1)
    file_data = assign_params(**{
        'Sha1': response.get('sha1'),
        'Sha256': response.get('sha256'),
        'Md5': response.get('md5'),
        'GlobalPrevalence': response.get('globalPrevalence'),
        'GlobalFirstObserved': response.get('globalFirstObserved'),
        'GlobalLastObserved': response.get('globalLastObserved'),
        'Size': response.get('size'),
        'FileType': response.get('fileType'),
        'IsPeFile': response.get('isPeFile'),
        'FilePublisher': response.get('filePublisher'),
        'FileProductName': response.get('fileProductName'),
        'Signer': response.get('signer'),
        'Issuer': response.get('issuer'),
        'SignerHash': response.get('signerHash'),
        'IsValidCertificate': response.get('isValidCertificate'),
        'DeterminationType': response.get('determinationType'),
        'DeterminationValue': response.get('determinationValue')
    })
    return file_data


def get_alert_related_ips_command():
    """Retrieves all IPs related to a specific alert.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    alert_id = demisto.args().get('id')
    response_ips_list = get_alert_related_ips_request(alert_id)
    ips_list = []
    for ip in response_ips_list['value']:
        ips_list.append(ip['id'])
    context_output = {
        'AlertID': alert_id,
        'IPs': ips_list
    }
    ec = {
        'MicrosoftATP.AlertIP(val.AlertID === obj.AlertID)': context_output
    }
    hr = 'Alert {} Related IPs:'.format(alert_id), ips_list
    return hr, ec, response_ips_list


def get_alert_related_domains_command():
    """Retrieves all domains related to a specific alert.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    alert_id = demisto.args().get('id')
    response_domains_list = get_alert_related_domains_request(alert_id)
    domains_list = []
    for domain in response_domains_list['value']:
        domains_list.append(domain['host'])
    context_output = {
        'AlertID': alert_id,
        'Domains': domains_list
    }
    ec = {
        'MicrosoftATP.AlertDomain(val.AlertID === obj.AlertID)': context_output
    }
    hr = 'Alert {} Related Domains:'.format(alert_id), domains_list
    return hr, ec, response_domains_list


def get_machine_action_by_id_command():
    """Returns machine's actions, if machine ID is None, return all actions
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    action_id = demisto.args().get('id', '')
    headers = ['ID', 'Type', 'Scope', 'Requestor', 'RequestorComment', 'Status', 'MachineID', 'ComputerDNSName',
               'CreationDateTimeUtc', 'LastUpdateTimeUtc', 'RelatedFileInfo', 'FileIdentifier', 'FileIdentifierType']
    if action_id:
        response = get_machine_action_by_id_request(action_id)
        action_data = get_action_data(action_id)
        hr = tableToMarkdown('Action {} Info:'.format(action_id), action_data, headers=headers)
        context_output = action_data
    else:
        response = get_machine_actions_request()
        actions_list = []
        for action in response['value']:
            actions_list.append(get_action_data(action['id']))
        hr = tableToMarkdown('Machine actions Info:', actions_list, headers=headers)
        context_output = actions_list
    ec = {
        'MicrosoftATP.MachineAction(val.ID === obj.ID)': context_output
    }
    return hr, ec, response


def get_action_data(action_id):
    """Get action ID and returns the action's info
    Returns:
        dict. Action's info
    """
    response = get_machine_action_by_id_request(action_id)
    action_data = {
            "ID": response.get('id'),
            "Type": response.get('type'),
            "Scope": response.get('scope'),
            "Requestor": response.get('requestor'),
            "RequestorComment": response.get('requestorComment'),
            "Status": response.get('status'),
            "MachineID": response.get('machineId'),
            "ComputerDNSName": response.get('computerDnsName'),
            "CreationDateTimeUtc": response.get('creationDateTimeUtc'),
            "LastUpdateTimeUtc": response.get('lastUpdateTimeUtc'),
            "RelatedFileInfo": {
                "FileIdentifier": response.get('fileIdentifier'),
                "FileIdentifierType": response.get('fileIdentifierType')
            }
        }
    return action_data


def get_machine_investigation_package_command():
    """Collect investigation package from a machine.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Type', 'Scope', 'Requestor', 'RequestorComment', 'Status', 'MachineID', 'ComputerDNSName',
               'CreationDateTimeUtc', 'LastUpdateTimeUtc', 'RelatedFileInfo', 'FileIdentifier', 'FileIdentifierType']
    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    response = get_investigation_package_request(machine_id, comment)
    action_data = get_action_data(response['id'])
    hr = tableToMarkdown('Initiating collect investigation package from {} machine :'.format(machine_id), action_data,
                         headers=headers)
    ec = {
        'MicrosoftATP.MachineAction(val.ID === obj.ID)': action_data
    }
    return hr, ec, response


def get_investigation_package_sas_uri_command():
    """Returns a URI that allows downloading an Investigation package.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['Link']
    action_id = demisto.args().get('action_id')
    response = get_investigation_package_sas_uri_request(action_id)
    link = {'Link': response['value']}
    hr = tableToMarkdown('success. This link is valid for a very short time and should be used immediately for'
                         ' downloading the package to a local storage. :', link, headers=headers)
    ec = {
        'MicrosoftATP.InvestigationURI(val.Link === obj.Link)': link
    }
    return hr, ec, response


def restrict_app_execution_command():
    """Restrict execution of all applications on the machine except a predefined set.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Type', 'Scope', 'Requestor', 'RequestorComment', 'Status', 'MachineID', 'ComputerDNSName',
               'CreationDateTimeUtc', 'LastUpdateTimeUtc', 'RelatedFileInfo', 'FileIdentifier', 'FileIdentifierType']
    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    response = restrict_app_execution_request(machine_id, comment)
    action_data = get_action_data(response['id'])
    hr = tableToMarkdown('Initiating Restrict execution of all applications on the machine {} except a predefined set:'
                         .format(machine_id), action_data, headers=headers)
    ec = {
        'MicrosoftATP.MachineAction(val.ID === obj.ID)': action_data
    }
    return hr, ec, response


def remove_app_restriction_command():
    """Enable execution of any application on the machine.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Type', 'Scope', 'Requestor', 'RequestorComment', 'Status', 'MachineID', 'ComputerDNSName',
               'CreationDateTimeUtc', 'LastUpdateTimeUtc', 'RelatedFileInfo', 'FileIdentifier', 'FileIdentifierType']
    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    response = remove_app_restriction_request(machine_id, comment)
    action_data = get_action_data(response['id'])
    hr = tableToMarkdown(
        'Removing applications restriction on the machine {}:'.format(machine_id), action_data, headers=headers)
    ec = {
        'MicrosoftATP.MachineAction(val.ID === obj.ID)': action_data
    }
    return hr, ec, response


def stop_and_quarantine_file_command():
    """Stop execution of a file on a machine and delete it.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Type', 'Scope', 'Requestor', 'RequestorComment', 'Status', 'MachineID', 'ComputerDNSName',
               'CreationDateTimeUtc', 'LastUpdateTimeUtc', 'RelatedFileInfo', 'FileIdentifier', 'FileIdentifierType']
    machine_id = demisto.args().get('machine_id')
    file_sha1 = demisto.args().get('sha1')
    comment = demisto.args().get('comment')
    response = stop_and_quarantine_file_request(machine_id, file_sha1, comment)
    action_data = get_action_data(response['id'])
    hr = tableToMarkdown(
        'Stopping the execution of a file on {} machine and deleting it:'.format(machine_id), action_data,
        headers=headers)
    ec = {
        'MicrosoftATP.MachineAction(val.ID === obj.ID)': action_data
    }
    return hr, ec, response


def get_investigations_by_id_command():
    """Returns the investigation info, if investigation ID is None, return all investigations
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    investigation_id = demisto.args().get('id', '')
    headers = ['ID', 'StartTime', 'EndTime', 'CancelledBy', 'InvestigationState', 'StatusDetails', 'MachineID',
               'ComputerDNSName', 'TriggeringAlertID']
    if investigation_id:
        response = get_investigation_by_id_request(investigation_id)
        investigation_data = get_investigation_data(investigation_id)
        hr = tableToMarkdown('Investigation {} Info:'.format(investigation_id), investigation_data, headers=headers)
        context_output = investigation_data
    else:
        response = get_investigation_list_request()
        investigations_list = []
        for investigation in response['value']:
            investigations_list.append(get_investigation_data(investigation['id']))
        hr = tableToMarkdown('Investigation Info:', investigations_list, headers=headers)
        context_output = investigations_list
    ec = {
        'MicrosoftATP.Investigation(val.ID === obj.ID)': context_output
    }
    return hr, ec, response


def get_investigation_data(investigation_id):
    """Get investigation ID and returns the investigation info
    Args:
        investigation_id: The investigation ID
    Returns:
        dict. Investigation's info
    """
    response = get_investigation_by_id_request(investigation_id)
    investigation_data = {
            "ID": response.get('id'),
            "StartTime": response.get('startTime'),
            "EndTime": response.get('endTime'),
            "CancelledBy": response.get('cancelledBy'),
            "InvestigationState": response.get('investigationState'),
            "StatusDetails": response.get('ststatusDetailsatus'),
            "MachineID": response.get('machineId'),
            "ComputerDNSName": response.get('computerDnsName'),
            "TriggeringAlertId": response.get('triggeringAlertId')
        }
    return investigation_data


def start_investigation_command():
    """Start automated investigation on a machine.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'StartTime', 'EndTime', 'CancelledBy', 'InvestigationState', 'StatusDetails', 'MachineID',
               'ComputerDNSName', 'TriggeringAlertID']
    machine_id = demisto.args().get('machine_id')
    comment = demisto.args().get('comment')
    response = start_investigation_request(machine_id, comment)
    investigation_id = response['id']
    investigation_data = get_investigation_data(investigation_id)
    hr = tableToMarkdown(
        'Starting investigation {} on {} machine:'.format(investigation_id, machine_id), investigation_data,
        headers=headers)
    ec = {
        'MicrosoftATP.Investigation(val.ID === obj.ID)': investigation_data
    }
    return hr, ec, response


def get_domain_statistics_command():
    """Retrieves the statistics on the given domain.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    domain = demisto.args().get('domain')
    response = get_domain_statistics_request(domain)
    domain_stat = {
        "Host": response.get('host'),
        "OrgPrevalence": response.get('orgPrevalence'),
        "OrgFirstSeen": response.get('orgFirstSeen'),
        "OrgLastSeen": response.get('orgLastSeen')
    }
    hr = tableToMarkdown(
        'Statistics on {} domain:'.format(domain), domain_stat)

    context_output = {
        'Domain': domain,
        'Statistics': domain_stat
    }
    ec = {
        'MicrosoftATP.DomainStatistics(val.Domain === obj.Domain)': context_output
    }
    return hr, ec, response


def get_domain_alerts_command():
    """Retrieves a collection of Alerts related to a given domain address.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Title', 'Description', 'IncidentID', 'Severity', 'Status', 'Classification', 'Category',
               'ThreatFamilyName', 'MachineID']
    domain = demisto.args().get('domain')
    response = get_domain_alerts_request(domain)
    alerts_list = []
    for alert in response['value']:
        alerts_list.append(get_alert_data(alert['id']))
    hr = tableToMarkdown('Domain {} related alerts Info:'.format(domain), alerts_list, headers=headers)
    context_output = {
        'Domain': domain,
        'Alerts': response['value']
    }
    ec = {
        'MicrosoftATP.DomainAlert(val.Domain === obj.Domain)': context_output
    }
    return hr, ec, response


def get_alert_data(alert_id):
    """Get investigation ID and returns the investigation info
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    response = get_alert_by_id_request(alert_id)
    alert_data = {
            "ID": response.get('id'),
            "IncidentID": response.get('incidentId'),
            "investigationID": response.get('investigationId'),
        "investigationState": response.get('investigationState'),
        "assignedTo": response.get('assignedTo'),
        "Severity": response.get('severity'),
        "Status": response.get('status'),
        "Classification": response.get('classification'),
        "determination": response.get('determination'),
        "detectionSource": response.get('detectionSource'),
        "Category": response.get('category'),
        "ThreatFamilyName": response.get('threatFamilyName'),
        "Title": response.get('title'),
            "Description": response.get('description'),
        "alertCreationTime": response.get('alertCreationTime'),
        "firstEventTime": response.get('firstEventTime'),
        "lastEventTime": response.get('lastEventTime'),
        "lastUpdateTime": response.get('lastUpdateTime'),
        "resolvedTime": response.get('resolvedTime'),
        "MachineID": response.get('machineId'),
        "comments": [
            {
                "Comment": response.get('comment'),
                "CreatedBy": response.get('createdBy'),
                "CreatedTime": response.get('createdTime')
            }
        ]

    }
    return alert_data


def get_domain_machine_command():
    """Retrieves a collection of Machines that have communicated to or from a given domain address.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'ComputerDNSName', 'OSPlatform','LastIPAddress', 'LastExternalIPAddress', 'HealthStatus',
               'RiskScore', 'ExposureScore']
    domain = demisto.args().get('domain')
    response = get_domain_machines_request(domain)
    machines_list = []
    for machine in response['value']:
        machines_list.append(get_machine_data(machine['id']))
    hr = tableToMarkdown('Machines that have communicated with {} domain:'.format(domain), machines_list,
                         headers=headers)
    context_output = {
        'Domain': domain,
        'Machines': machines_list
    }
    ec = {
        'MicrosoftATP.DomainMachine(val.Domain === obj.Domain)': context_output
    }
    return hr, ec, response


def get_machine_data(machine_id):
    """Get machine ID and returns the machine's info
    Returns:
        dict. machine's info
    """
    machine = get_machine_details_request(machine_id)
    machine_data = assign_params(**{
        'ComputerDNSName': machine.get('computerDnsName'),
        'ID': machine.get('id'),
        'AgentVersion': machine.get('agentVersion'),
        'FirstSeen': machine.get('firstSeen'),
        'LastSeen': machine.get('lastSeen'),
        'HealthStatus': machine.get('healthStatus'),
        'IsAADJoined': machine.get('isAadJoined'),
        'LastExternalIPAddress': machine.get('lastExternalIpAddress'),
        'LastIPAddress': machine.get('lastIpAddress'),
        'Tags': machine.get('machineTags'),
        'OSBuild': machine.get('osBuild'),
        'OSPlatform': machine.get('osPlatform'),
        'RBACGroupID': machine.get('rbacGroupId'),
        'RiskScore': machine.get('riskScore'),
        'RBACGroupName':machine.get('rbacGroupName'),
        'AADDeviceID':machine.get('aadDeviceId'),
        'OSVersion': machine.get('osVersion'),
        'ExposureScore': machine.get('exposureScore')
    })
    return machine_data


def get_file_statistics_command():
    """Retrieves the statistics on the given file.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    file_sha1 = demisto.args().get('sha1')
    response = get_file_statistics_request(file_sha1)
    file_name = response.get('topFileNames')
    file_stat = assign_params(**{
        "Sha1": response.get('sha1'),
        "OrgPrevalence": response.get('orgPrevalence'),
        "OrgFirstSeen": response.get('orgFirstSeen'),
        "OrgLastSeen": response.get('orgLastSeen'),
        "GlobalPrevalence": response.get('globalPrevalence'),
        "GlobalFirstObserved": response.get('globalFirstObserved'),
        "GlobalLastObserved": response.get('globalLastObserved'),
        "TopFileNames": [file_name],
    })
    hr = tableToMarkdown(
        'Statistics on {} file:'.format(file_name), file_stat)
    context_output = {
        'Sha1': file_sha1,
        'Statistics': file_stat
    }
    ec = {
        'MicrosoftATP.FileStatistics(val.Sha1 === obj.Sha1)': context_output
    }
    return hr, ec, response


def get_file_alerts_command():
    """Retrieves a collection of Alerts related to a given file hash.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Title', 'Description', 'IncidentID', 'Severity', 'Status', 'Classification', 'Category',
               'ThreatFamilyName', 'MachineID']
    file_sha1 = demisto.args().get('sha1')
    response = get_file_alerts_request(file_sha1)
    alerts_list = []
    for alert in response['value']:
        alerts_list.append(get_alert_data(alert['id']))
    hr = tableToMarkdown('File {} related alerts Info:'.format(file_sha1), alerts_list, headers=headers)
    context_output = {
        'Sha1': file_sha1,
        'Alerts': alerts_list
    }
    ec = {
        'MicrosoftATP.FileAlert(val.Sha1 === obj.Sha1)': context_output
    }
    return hr, ec, response


def get_ip_statistics_command():
    """Retrieves the statistics on the given IP.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    ip = demisto.args().get('ip')
    response = get_ip_statistics_request(ip)
    ip_stat = assign_params(**{
        "IPAddress": response.get('ipAddress'),
        "OrgPrevalence": response.get('orgPrevalence'),
        "OrgFirstSeen": response.get('orgFirstSeen'),
        "OrgLastSeen": response.get('orgLastSeen')
    })
    hr = tableToMarkdown(
        'Statistics on {} IP:'.format(ip), ip_stat)
    context_output = {
        'IPAddress': ip,
        'Statistics': ip_stat
    }
    ec = {
        'MicrosoftATP.IPStatistics(val.IPAddress === obj.IPAddress)': context_output
    }
    return hr, ec, response


def get_ip_alerts_command():
    """Retrieves a collection of Alerts related to a given IP.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Title', 'Description', 'IncidentID', 'Severity', 'Status', 'Classification', 'Category',
               'ThreatFamilyName', 'MachineID']
    ip = demisto.args().get('ip')
    response = get_ip_alerts_request(ip)
    alerts_list = []
    for alert in response['value']:
        alerts_list.append(get_alert_data(alert['id']))
    hr = tableToMarkdown('IP {} related alerts Info:'.format(ip), alerts_list, headers=headers)
    context_output = {
        'IPAddress': ip,
        'Alerts': alerts_list
    }
    ec = camelize({
        'MicrosoftATP.IPAlert(val.IPAddress === obj.IPAddress)': context_output
    })
    return hr, ec, response


def get_user_alerts_command():
    """Retrieves a collection of Alerts related to a given user ID.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'Title', 'Description', 'IncidentID', 'Severity', 'Status', 'Classification', 'Category',
               'ThreatFamilyName', 'MachineID']
    username = demisto.args().get('username')
    response = get_user_alerts_request(username)
    alerts_list = []
    for alert in response['value']:
        alerts_list.append(get_alert_data(alert['id']))
    hr = tableToMarkdown('User {} related alerts Info:'.format(username), alerts_list, headers=headers)
    context_output = {
        'Username': username,
        'Alerts': alerts_list
    }
    ec = {
        'MicrosoftATP.UserAlert(val.Username === obj.Username)': context_output
    }
    return hr, ec, response


def get_user_machine_command():
    """Retrieves a collection of machines related to a given user ID.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'ComputerDNSName', 'OSPlatform','LastIPAddress', 'LastExternalIPAddress', 'HealthStatus',
               'RiskScore', 'ExposureScore']
    username = demisto.args().get('username')
    response = get_user_machines_request(username)
    machines_list = []
    for machine in response['value']:
        machines_list.append(get_machine_data(machine['id']))
    hr = tableToMarkdown('Machines that are related to user {}:'.format(username), machines_list, headers=headers)
    context_output = {
        'Username': username,
        'Machines': machines_list
    }
    ec = {
        'MicrosoftATP.UserMachine(val.Username === obj.Username)': context_output
    }
    return hr, ec, response


def add_remove_machine_tag_command():
    """Adds or remove tag to a specific Machine.
    Returns:
        (str, dict, dict). Human readable, context, raw response
    """
    headers = ['ID', 'ComputerDNSName', 'OSPlatform','LastIpAddress', 'LastExternalIPAddress', 'HealthStatus',
               'RiskScore', 'ExposureScore']
    machine_id = demisto.args().get('machine_id')
    action = demisto.args().get('action')
    tag = demisto.args().get('tag')
    response = add_remove_machine_tag_request(machine_id, action, tag)
    machine_data = get_machine_data(machine_id)
    hr = tableToMarkdown('Succeed to {} tag to {}:'.format(action, machine_id), machine_data, headers=headers)
    ec = {
        'MicrosoftATP.Machine(val.ID === obj.ID)': machine_data
    }
    return hr, ec, response


def fetch_incidents():
    last_run = demisto.getLastRun()

    if last_run and last_run['last_alert_fetched_time']:
        last_alert_fetched_time = datetime.strptime(last_run['last_alert_fetched_time'], '%Y-%m-%dT%H:%M:%S.%f')
    else:
        last_alert_fetched_time = datetime.now() - timedelta(days=300)

    previous_ids = last_run.get('last_ids', [])
    latest_creation_time = last_alert_fetched_time

    alerts = list_alerts()['value']
    incidents = []
    last_ids = []

    for alert in alerts:
        # Removing 'Z' from timestamp and converting to datetime
        alert_creation_time = datetime.strptime(alert['alertCreationTime'][:-2], '%Y-%m-%dT%H:%M:%S.%f')
        alert_status = alert['status']
        alert_severity = alert['severity']
        if alert_creation_time >= last_alert_fetched_time and alert_status in FETCH_STATUS and \
                alert_severity in FETCH_SEVERITY and alert['id'] not in previous_ids:
            incident = alert_to_incident(alert)
            incidents.append(incident)
            if alert_creation_time == latest_creation_time:
                last_ids.append(alert["id"])
            if alert_creation_time > latest_creation_time:
                latest_creation_time = alert_creation_time
                last_ids = [alert['id']]

    if not last_ids:
        last_ids = previous_ids

    demisto.setLastRun({
        'last_alert_fetched_time': datetime.strftime(latest_creation_time, '%Y-%m-%dT%H:%M:%S.%f'),
        "last_ids": last_ids

    })
    demisto.incidents(incidents)


def test_function():
    token = get_access_token()
    response = requests.get(
        BASE_URL + '/alerts',
        headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        params={'$top': '1'},
        verify=USE_SSL
    )
    try:
        _ = response.json() if response.text else {}
        if not response.ok:
            return_error(f'API call to Windows Advanced Threat Protection. '
                         f'Please check authentication related parameters. '
                         f'[{response.status_code}] - {response.reason}')

        demisto.results('ok')

    except TypeError as ex:
        demisto.debug(str(ex))
        return_error(f'API call to Windows Advanced Threat Protection failed, could not parse result. '
                     f'Please check authentication related parameters. [{response.status_code}]')


''' EXECUTION CODE '''

LOG('command is %s' % (demisto.command(), ))

try:
    if demisto.command() == 'test-module':
        test_function()

    elif demisto.command() == 'fetch-incidents':
        fetch_incidents()

    elif demisto.command() == 'microsoft-atp-isolate-machine':
        isolate_machine_command()

    elif demisto.command() == 'microsoft-atp-unisolate-machine':
        unisolate_machine_command()

    elif demisto.command() == 'microsoft-atp-get-machines':
        get_machines_command()

    elif demisto.command() == 'microsoft-atp-get-file-related-machines':
        get_file_related_machines_command()

    elif demisto.command() == 'microsoft-atp-get-machine-details':
        get_machine_details_command()

    elif demisto.command() == 'microsoft-atp-block-file':
        block_file_command()

    elif demisto.command() == 'microsoft-atp-run-antivirus-scan':
        run_antivirus_scan_command()

    elif demisto.command() == 'microsoft-atp-list-alerts':
        list_alerts_command()

    elif demisto.command() == 'microsoft-atp-update-alert':
        update_alert_command()

    elif demisto.command() == 'microsoft-atp-advanced-hunting':
        get_advanced_hunting_command()

    elif demisto.command() == 'microsoft-atp-create-alert':
        create_alert_command()

    elif demisto.command() == 'microsoft-atp-get-alert-related-files':
        return_outputs(*get_alert_related_files_command())

    elif demisto.command() == 'microsoft-atp-get-alert-related-ips':
        return_outputs(*get_alert_related_ips_command())

    elif demisto.command() == 'microsoft-atp-get-alert-related-domains':
        return_outputs(*get_alert_related_domains_command())

    elif demisto.command() == 'microsoft-atp-list-machine-actions':
        return_outputs(*get_machine_action_by_id_command())

    elif demisto.command() == 'microsoft-atp-collect-investigation-package':
        return_outputs(*get_machine_investigation_package_command())

    elif demisto.command() == 'microsoft-atp-get-investigation-package-sas-uri':
        return_outputs(*get_investigation_package_sas_uri_command())

    elif demisto.command() == 'microsoft-atp-restrict-app-execution':
        return_outputs(*restrict_app_execution_command())

    elif demisto.command() == 'microsoft-atp-remove-app-restriction':
        return_outputs(*remove_app_restriction_command())

    elif demisto.command() == 'microsoft-atp-stop-and-quarantine-file':
        return_outputs(*stop_and_quarantine_file_command())

    elif demisto.command() == 'microsoft-atp-list-investigations':
        return_outputs(*get_investigations_by_id_command())

    elif demisto.command() == 'microsoft-atp-start-investigation':
        return_outputs(*start_investigation_command())

    elif demisto.command() == 'microsoft-atp-get-domain-statistics':
        return_outputs(*get_domain_statistics_command())

    elif demisto.command() == 'microsoft-atp-get-domain-alerts':
        return_outputs(*get_domain_alerts_command())

    elif demisto.command() == 'microsoft-atp-get-domain-machines':
        return_outputs(*get_domain_machine_command())

    elif demisto.command() == 'microsoft-atp-get-file-statistics':
        return_outputs(*get_file_statistics_command())

    elif demisto.command() == 'microsoft-atp-get-file-alerts':
        return_outputs(*get_file_alerts_command())

    elif demisto.command() == 'microsoft-atp-get-ip-statistics':
        return_outputs(*get_ip_statistics_command())

    elif demisto.command() == 'microsoft-atp-get-ip-alerts':
        return_outputs(*get_ip_alerts_command())

    elif demisto.command() == 'microsoft-atp-get-user-alerts':
        return_outputs(*get_user_alerts_command())

    elif demisto.command() == 'microsoft-atp-get-user-machines':
        return_outputs(*get_user_machine_command())

    elif demisto.command() == 'microsoft-atp-add-remove-machine-tag':
        return_outputs(*add_remove_machine_tag_command())
except Exception as e:
    return_error(str(e))
