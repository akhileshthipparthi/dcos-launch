import os

import cerberus
import yaml

from dcos_launch import util
from dcos_launch.platforms import aws, gcp


def expand_path(path: str, relative_dir: str) -> str:
    """ Returns an absolute path by performing '~' and '..' substitution target path

    path: the user-provided path
    relative_dir: the absolute directory to which `path` should be seen as
        relative
    """
    path = os.path.expanduser(path)
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(relative_dir, path))


def load_config(config_path: str) -> dict:
    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as ex:
        raise util.LauncherError('InvalidYaml', None) from ex
    except FileNotFoundError as ex:
        raise util.LauncherError('MissingConfig', None) from ex


def validate_url(field, value, error):
    if not value.startswith('http'):
        error(field, 'Not a valid HTTP URL')


def load_ssh_private_key(doc):
    if doc.get('key_helper') == 'true':
        return 'unset'
    if 'ssh_private_key_filename' not in doc:
        return util.NO_TEST_FLAG
    return util.read_file(doc['ssh_private_key_filename'])


class LaunchValidator(cerberus.Validator):
    """ Needs to use unintuitive pattern so that child validator can be created
    for validated the nested dcos_config. See:
    http://docs.python-cerberus.org/en/latest/customize.html#instantiating-custom-validators
    """
    def __init__(self, *args, **kwargs):
        super(LaunchValidator, self).__init__(*args, **kwargs)
        assert 'config_dir' in kwargs, 'This class must be supplied with the config_dir kwarg'
        self.config_dir = kwargs['config_dir']

    def _normalize_coerce_expand_local_path(self, value):
        return expand_path(value, self.config_dir)


def _expand_error_dict(errors: dict) -> str:
    message = ''
    for key, errors in errors.items():
        sub_message = 'Field: {}, Errors: '.format(key)
        for e in errors:
            if isinstance(e, dict):
                sub_message += _expand_error_dict(e)
            else:
                sub_message += e
            sub_message += '\n'
        message += sub_message
    return message


def _raise_errors(validator: LaunchValidator):
    message = _expand_error_dict(validator.errors)
    raise util.LauncherError('ValidationError', message)


def get_validated_config_from_path(config_path: str) -> dict:
    config = load_config(config_path)
    config_dir = os.path.dirname(config_path)
    return get_validated_config(config, config_dir)


def get_validated_config(user_config: dict, config_dir: str) -> dict:
    """ Returns validated a finalized argument dictionary for dcos-launch
    Given the huge range of configuration space provided by this configuration
    file, it must be processed in three steps (common, provider-specifc,
    platform-specific)
    Args:
        use_config: options (perhaps incomplete) provided by the user
        config_dir: path for the config file for resolving relative
            file links
    """
    # validate against the fields common to all configs
    validator = LaunchValidator(COMMON_SCHEMA, config_dir=config_dir, allow_unknown=True)
    if not validator.validate(user_config):
        _raise_errors(validator)

    # add provider specific information to the basic validator
    provider = validator.normalized(user_config)['provider']
    if provider == 'onprem':
        validator.schema.update(ONPREM_DEPLOY_COMMON_SCHEMA)
    else:
        validator.schema.update(TEMPLATE_DEPLOY_COMMON_SCHEMA)

    # validate again before attempting to add platform information
    if not validator.validate(user_config):
        _raise_errors(validator)

    # use the intermediate provider-validated config to add the platform schema
    platform = validator.normalized(user_config)['platform']
    if platform == 'aws':
        validator.schema.update({
            'aws_region': {
                'type': 'string',
                'required': True,
                'default_setter': lambda doc: util.set_from_env('AWS_REGION')},
            'disable_rollback': {
                'type': 'boolean',
                'required': False,
                'default': False}})
        if provider == 'onprem':
            validator.schema.update(AWS_ONPREM_SCHEMA)
    elif platform in ('gcp', 'gce'):
        validator.schema.update({
            'gce_zone': {
                'type': 'string',
                'required': True,
                'default_setter': lambda doc: util.set_from_env('GCE_ZONE')}})
        # only use gcp here on out
        user_config['platform'] = 'gcp'
        if provider == 'onprem':
            validator.schema.update(GCP_ONPREM_SCHEMA)
    elif platform == 'azure':
        validator.schema.update({
            'azure_location': {
                'type': 'string',
                'required': True,
                'default_setter': lambda doc: util.set_from_env('AZURE_LOCATION')}})
    else:
        raise NotImplementedError()

    # do final validation
    validator.allow_unknown = False
    if not validator.validate(user_config):
        _raise_errors(validator)
    return validator.normalized(user_config)


COMMON_SCHEMA = {
    'deployment_name': {
        'type': 'string',
        'required': True},
    'provider': {
        'type': 'string',
        'required': True,
        'allowed': [
            'aws',
            'azure',
            'onprem']},
    'launch_config_version': {
        'type': 'integer',
        'required': True,
        'allowed': [1]},
    'ssh_port': {
        'type': 'integer',
        'required': False,
        'default': 22},
    'ssh_private_key_filename': {
        'type': 'string',
        'coerce': 'expand_local_path',
        'required': False},
    'ssh_private_key': {
        'type': 'string',
        'required': False,
        'default_setter': load_ssh_private_key},
    'ssh_user': {
        'type': 'string',
        'required': False,
        'default': 'core'},
    'key_helper': {
        'type': 'boolean',
        'default': False},
    'zen_helper': {
        'type': 'boolean',
        'default': False},
    'tags': {
        'type': 'dict',
        'required': False}}


TEMPLATE_DEPLOY_COMMON_SCHEMA = {
    # platform MUST be equal to provider when using templates
    'platform': {
        'type': 'string',
        'readonly': True,
        'default_setter': lambda doc: doc['provider']},
    'template_url': {
        'type': 'string',
        'required': True,
        'validator': validate_url},
    'template_parameters': {
        'type': 'dict',
        'required': True}}


def _validate_fault_domain_helper(field, value, error):
    for fd in value:
        pass

    if not value.startswith('http'):
        error(field, 'Not a valid HTTP URL')


def _get_num_agents(doc: dict, field_name: str):
    total = 0
    for fd in doc.get('fault_domain_helper', list()):
        total += fd[field_name]
    return total


ONPREM_DEPLOY_COMMON_SCHEMA = {
    'platform': {
        'type': 'string',
        'required': True,
        # allow gce but remap it to GCP during validation
        'allowed': ['aws', 'gcp', 'gce']},
    'installer_url': {
        'validator': validate_url,
        'type': 'string',
        'required': True},
    'installer_port': {
        'type': 'integer',
        'default': 9000},
    'num_private_agents': {
        'type': 'integer',
        'required': False,
        'min': 0,
        # note: cannot assume nested schema values will be populated with defaults
        #   when the default setter runs
        'default_setter': lambda doc: sum(
            [v.get('num_private_agents',  0) for v in doc['fault_domain_helper'].values()])},
    'num_public_agents': {
        'type': 'integer',
        'required': False,
        'min': 0,
        # note: cannot assume nested schema values will be populated with defaults
        #   when the default setter runs
        'default_setter': lambda doc: sum(
            [v.get('num_public_agents',  0) for v in doc['fault_domain_helper'].values()])},
    'num_masters': {
        'type': 'integer',
        'allowed': [1, 3, 5, 7, 9],
        'required': True},
    'dcos_config': {
        'type': 'dict',
        'required': True,
        'allow_unknown': True,
        'schema': {
            'ip_detect_filename': {
                'coerce': 'expand_local_path',
                'excludes': 'ip_detect_content'},
            'ip_detect_public_filename': {
                'coerce': 'expand_local_path',
                'excludes': 'ip_detect_public_content'},
            'ip_detect_contents': {
                'excludes': 'ip_detect_filename'},
            'ip_detect_public_contents': {
                'excludes': 'ip_detect_public_filename'},
            # currently, these values cannot be set by a user, only by the launch process
            'master_list': {'readonly': True},
            'agent_list': {'readonly': True},
            'public_agent_list': {'readonly': True},
            'fault_domain_script_filename': {
                'coerce': 'expand_local_path',
                'excludes': 'fault_domain_script_contents'}}},
    'fault_domain_helper': {
        'type': 'dict',
        'required': False,
        'keyschema': {'type': 'string'},
        'valueschema': {
            'type': 'dict',
            'schema': {
                'num_zones': {
                    'required': True,
                    'type': 'integer',
                    'default': 1},
                'num_private_agents': {
                    'required': True,
                    'type': 'integer',
                    'default': 0},
                'num_public_agents': {
                    'required': True,
                    'type': 'integer',
                    'default': 0}
                }
            }
        }
    }


AWS_ONPREM_SCHEMA = {
    'aws_key_name': {
        'type': 'string',
        'dependencies': {
            'key_helper': False}},
    'os_name': {
        'type': 'string',
        # not required because machine image can be set directly
        'required': False,
        'default': 'cent-os-7-dcos-prereqs',
        'allowed': list(aws.OS_AMIS.keys())},
    'bootstrap_os_name': {
        'type': 'string',
        'required': False,
        # bootstrap node requires docker to be installed
        'default': 'cent-os-7-dcos-prereqs',
        'allowed': list(aws.OS_AMIS.keys())},
    'instance_ami': {
        'type': 'string',
        'required': True,
        'default_setter': lambda doc: aws.OS_AMIS[doc['os_name']][doc['aws_region']]},
    'instance_type': {
        'type': 'string',
        'required': True},
    'bootstrap_instance_ami': {
        'type': 'string',
        'required': True,
        'default_setter': lambda doc: aws.OS_AMIS[doc['bootstrap_os_name']][doc['aws_region']]},
    'bootstrap_instance_type': {
        'type': 'string',
        'required': True,
        'default': 'm4.xlarge'},
    'admin_location': {
        'type': 'string',
        'required': True,
        'default': '0.0.0.0/0'},
    'ssh_user': {
        'required': True,
        'type': 'string',
        'default_setter': lambda doc: aws.OS_SSH_INFO[doc['os_name']].user}}


def deduce_image_project(doc: dict):
    src_image = doc['source_image']
    if 'centos' in src_image or 'cent-os' in src_image:
        return 'centos-cloud'
    if 'rhel' in src_image:
        return 'rhel-cloud'
    if 'ubuntu' in src_image:
        return 'ubuntu-os-cloud'
    if 'coreos' in src_image:
        return 'coreos-cloud'
    if 'debian' in src_image:
        return 'debian-cloud'

    raise util.LauncherError('ValidationError', """Couldn't deduce the image project for your source image. Please
                             specify the "image_project" parameter in your dcos-launch config. Possible values are:
                             centos-cloud, rhel-cloud, ubuntu-os-cloud, coreos-cloud and debian-cloud.""")


GCP_ONPREM_SCHEMA = {
    'machine_type': {
        'type': 'string',
        'required': False,
        'default': 'n1-standard-4'},
    'os_name': {
        # To see all image families: https://cloud.google.com/compute/docs/images
        'type': 'string',
        'required': False,
        'default': 'coreos'},
    'source_image': {
        'type': 'string',
        'required': False,
        'default_setter': lambda doc: 'family/' + gcp.OS_IMAGE_FAMILIES.get(doc['os_name'], doc['os_name'])},
    'image_project': {
        'type': 'string',
        'required': False,
        'default_setter': deduce_image_project},
    'ssh_public_key': {
        'type': 'string',
        'required': False},
    'disk_size': {
        'type': 'integer',
        'required': False,
        'default': 42},
    'disk_type': {
        'type': 'string',
        'required': False,
        'default': 'pd-ssd'},
    'disable_updates': {
        'type': 'boolean',
        'required': False,
        'default': False}}
