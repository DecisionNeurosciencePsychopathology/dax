
import copy
import itertools
import logging
import sys
from collections import namedtuple

from .task import NeedInputsException
from .task import NEED_INPUTS, OPEN_STATUS_LIST, BAD_QA_STATUS,\
    JOB_PENDING, REPROC, RERUN, FAILED_NEEDS_REPROC, NEEDS_QA
from . import XnatUtils
from . import utilities

LOGGER = logging.getLogger('dax')

select_namespace = {
    'foreach': {'args': [{'optional': True, 'type': str}]},
    'one': {'args': []},
    'some': {'args': [{'optional': False, 'type': int}]},
    'all': {'args': []},
    'from': {'args': [{'optional': False, 'type': str}]}
}

select_session_namespace = {
    'current': {'args': []},
    'prior': {'args': [{'optional': False, 'type': int}]},
    'prior-with': {'args': [{'optional': False, 'type': int}]},
    'first': {'args': []},
    'first-with': {'args': []}
}


no_scans_error = 'No scan of the required type/s ({}) found for input {}'
no_asrs_error = 'No assessors of the require type/s ({}) found for input {}'
scan_unusable_error = 'Scan {} is unusable for input {}'
asr_unusable_error = 'Assessor {} is unusable for input {}'

missing_field_unnamed = 'Error: {} at position {} is missing {} field'
missing_field_named = "Error: {} '{}' is missing '{}' field"
bad_mode = ("Error: {} '{}': '{}' has an invalid value '{}'. "
            "It must be one of {}")
missing_resource_field_unnamed = \
    "Error in {} '{}': missing {} from resource at position {}"
missing_resource_field_named = \
    "Error in {} '{}': missing {} from resource field '{}'"
bad_resource_mode = \
    "Error in {} '{}'; resource field '{}' has an invalid value"

resource_paths = {
    'assessor': '{0}/out/resources/{1}',
    'scan': '{0}/resources/{1}'
}

uri_paths = {
    'assessor': '{0}/data{1}/out/resources/{2}',
    'scan': '{0}/data{1}/resources/{2}'
}


# parser pipeline
# . check whether artefacts of the appropriate type are present for a given
#   assessor
# . if they are, map them to inputs with the appropriate iteration
#   . if no foreach select statements are present, generate one set of command
#     parameters
#   . if one or more foreach select statements are present, generate the
#     appropriate cartesian product of command parameters
# . for each set of command parameters generated, create an assessor depending
#   on the state of the artefacts listed in the command parameters
#   . if one or more artefacts are of inappropriate quality

class ParserArtefact:
    def __init__(self, path, resources, entity):
        self.name = path.split('/')[-1]
        self.path = path
        self.resources = resources,
        self.entity = entity

    def __repr__(self):
        return '{}(path = {}, resources = {}, entity = {})'.format(
            self.__class__.__name__, self.path, self.resources, self.entity
        )


class SelectSessionParameters:
    def __init__(self, mode, delta):
        self.mode = mode
        self.delta = delta

    def __repr__(self):
        return '{}(mode = {}, delta = {})'.format(self.__class__.__name__,
                                                  self.mode,
                                                  self.delta)


TimestampSession = namedtuple('TimestampSession', 'timestamp, session')

ArtefactEntry = namedtuple('ArtefactEntry', 'path, type, object')


class ProcessorParser:

    __schema_dict_v1 = {
        'top': set(['schema', 'inputs', 'xnat', 'attrs']),
        'xnat': set(['scans', 'assessors']),
        'scans': set(['select', 'types', 'nargs', 'resources', 'needs_qc']),
        'assessors': set(['select', 'types', 'nargs', 'resources', 'needs_qc']),
        'resources': set(['resource', 'varname', 'required'])
    }

    def __init__(self, yaml_source, proctype=None):
        self.yaml_source = yaml_source

        (self.inputs, self.inputs_by_type, self.iteration_sources,
            self.iteration_map, self.prior_session_count) =\
            ProcessorParser.parse_inputs(yaml_source)

        self.match_filters = ProcessorParser.parse_match_filters(yaml_source)
        self.variables_to_inputs = ProcessorParser.parse_variables(self.inputs)

        self.csess = None
        self.sessions_ = None
        self.artefacts = None
        self.artefacts_by_input = None
        self.parameter_matrix = None
        self.assessor_parameter_map = None

        self.xsitype = yaml_source['attrs'].get('xsitype', 'proc:genProcData')

        if proctype:
            self.proctype = proctype
        else:
            self.proctype = XnatUtils.get_proctype(
                yaml_source['inputs']['default']['spider_path'])[0]

        self.is_longitudinal_ = ProcessorParser.is_longitudinal(yaml_source)

    def parse_session(self, csess, sessions):
        """
        Parse a session to determine whether new assessors should be created.
        This call populates assessor_parameter_map.
        :param csess: the session in question
        :param sessions: the full list of sessions, including csess, for the
        subject
        :return: None
        """
        self.csess = None
        self.sessions_ = sessions
        self.artefacts = None
        self.artefacts_by_input = None
        self.parameter_matrix = None
        self.assessor_parameter_map = None

        for i in range(len(sessions) - 1):
            if sessions[i].creation_timestamp_ <\
                  sessions[i+1].creation_timestamp_:
                raise ValueError("session param is not ordered by datetime")

        if not self.is_longitudinal_:
            relevant_sessions = [csess]
        else:
            index = sessions.index(csess)
            relevant_sessions = sessions[index:]

        artefacts = ProcessorParser.parse_artefacts(relevant_sessions)

        artefacts_by_input = \
            ProcessorParser.map_artefacts_to_inputs(relevant_sessions,
                                                    self.inputs,
                                                    self.inputs_by_type)

        parameter_matrix = \
            ProcessorParser.generate_parameter_matrix(
                self.inputs,
                self.iteration_sources,
                self.iteration_map,
                artefacts,
                artefacts_by_input)

        parameter_matrix = ProcessorParser.filter_matrix(
            parameter_matrix,
            self.match_filters,
            artefacts)

        assessor_parameter_map = \
            ProcessorParser.compare_to_existing(relevant_sessions,
                                                self.proctype,
                                                parameter_matrix)

        self.csess = csess
        self.artefacts = artefacts
        self.artefacts_by_input = artefacts_by_input
        self.parameter_matrix = parameter_matrix
        self.assessor_parameter_map = list(assessor_parameter_map)

    def get_variable_set(self, assr):
        assr_inputs = XnatUtils.get_assessor_inputs(assr)

        # map from parameters to input resources
        command_set = dict()
        for k, v in list(self.variables_to_inputs.items()):
            inp = self.inputs[v['input']]
            artefact_type = inp['artefact_type']
            resource = v['resource']

            path_elements = [assr_inputs[v['input']], resource]

            command_set[k] =\
                resource_paths[artefact_type].format(*path_elements)

        return command_set

    def find_inputs(self, assr, sessions, assr_inputs):
        # LOGGER.debug('getting inputs from xnat')
        # assr_inputs = {
        #    key.decode(): val.decode() for key, val in
        #    list(XnatUtils.get_assessor_inputs(assr).items())}
        # LOGGER.debug('finished getting inputs')

        variable_set = {}
        input_list = []

        LOGGER.debug('checking status of each artefact')
        # Check artefact status
        for artk, artv in list(assr_inputs.items()):
            LOGGER.debug('checking status:'+artv)
            inp = self.inputs[artk]
            art_type = inp['artefact_type']

            if art_type == 'scan' and not inp['needs_qc']:
                continue

            if art_type == 'scan':
                qstatus = XnatUtils.get_scan_status(sessions, artv)
                if qstatus == 'unusable':
                    raise NeedInputsException(artk + ': Not Usable')
            else:
                pstatus, qstatus = XnatUtils.get_assr_status(sessions, artv)
                if pstatus in OPEN_STATUS_LIST + [NEED_INPUTS]:
                    raise NeedInputsException(artk + ': Not Ready')

                if qstatus in [JOB_PENDING, REPROC, RERUN]:
                    raise NeedInputsException(artk + ': Not Ready')

                if not inp['needs_qc']:
                    continue

                if (qstatus in [FAILED_NEEDS_REPROC, NEEDS_QA]):
                    raise NeedInputsException(artk + ': Needs QC')

                for badstatus in BAD_QA_STATUS:
                    if badstatus.lower() in qstatus.split(' ')[0].lower():
                        raise NeedInputsException(artk + ': Bad QC')

        LOGGER.debug('mapping params to artefact resources')
        # map from parameters to input resources
        for k, v in list(self.variables_to_inputs.items()):
            LOGGER.debug('mapping:'+k)
            inp = self.inputs[v['input']]
            artefact_type = inp['artefact_type']
            resource = v['resource']

            # Find the resource
            cur_res = None
            for inp_res in inp['resources']:
                if inp_res['varname'] == k:
                    cur_res = inp_res
                    break

            if isinstance(assr_inputs[v['input']], list):
                for vnum, vinput in enumerate(assr_inputs[v['input']]):
                    robj = assr._intf.select(
                        resource_paths[artefact_type].format(
                            vinput, resource))

                    if not robj.exists():
                        LOGGER.debug('failed to find resource')
                        raise NeedInputsException('No Resource')

                    if 'fmatch' in cur_res:
                        fmatch = cur_res['fmatch']
                    elif cur_res['ftype'] == 'FILE':
                        # Default to all
                        fmatch = '*'
                    else:
                        fmatch = None

                    if 'filepath' in cur_res:
                        fpath = cur_res['filepath']
                        res_path = resource + '/files/' + fpath
                    elif fmatch:
                        # Get list of all files in the resource
                        file_list = robj.files().get()

                        # Filter list based on regex matching
                        regex = utilities.extract_exp(fmatch, full_regex=False)
                        file_list = [x for x in file_list if regex.match(x)]

                        # Make a comma separated list of files
                        uri_list = ['{}/files/{}'.format(
                            resource, f) for f in file_list]
                        res_path = ','.join(uri_list)
                    else:
                        res_path = resource + '/files'

                    path_elements = [
                        assr._intf.host,
                        vinput,
                        res_path
                    ]

                    variable_set[k] = uri_paths[artefact_type].format(
                        *path_elements)

                    # Append to inputs to be downloaded
                    input_list.append({
                        'fdest': str(vnum) + cur_res['fdest'],
                        'ftype': cur_res['ftype'],
                        'fpath': variable_set[k]
                    })
            else:
                # Select the resource object on xnat
                robj = assr._intf.select(resource_paths[artefact_type].format(
                   assr_inputs[v['input']], resource))

                if not robj.exists():
                    LOGGER.debug('failed to find resource')
                    raise NeedInputsException('No Resource')

                if 'fmatch' in cur_res:
                    fmatch = cur_res['fmatch']
                elif cur_res['ftype'] == 'FILE':
                    # Default to all
                    fmatch = '*'
                else:
                    fmatch = None

                if 'filepath' in cur_res:
                    fpath = cur_res['filepath']
                    res_path = resource + '/files/' + fpath
                elif fmatch:
                    # Get list of all files in the resource
                    file_list = robj.files().get()

                    # Filter list based on regex matching
                    regex = utilities.extract_exp(fmatch, full_regex=False)
                    file_list = [x for x in file_list if regex.match(x)]

                    # Make a comma separated list of files
                    uri_list = ['{}/files/{}'.format(
                        resource, f) for f in file_list]
                    res_path = ','.join(uri_list)
                else:
                    res_path = resource + '/files'

                path_elements = [
                    assr._intf.host,
                    assr_inputs[v['input']],
                    res_path
                ]

                variable_set[k] = uri_paths[artefact_type].format(
                    *path_elements)

                # Append to inputs to be downloaded
                input_list.append({
                    'fdest': cur_res['fdest'],
                    'ftype': cur_res['ftype'],
                    'fpath': variable_set[k]
                })
                # Replace path with destination path after download
                if 'varname' in cur_res:
                    variable_set[k] = cur_res['fdest']

        LOGGER.debug('finished mapping params to artefact resources')

        return variable_set, input_list

    @staticmethod
    def _get_yaml_checker(version):
        if version == '1':
            return ProcessorParser.__check_yaml_v1
        return None

    @staticmethod
    def _get_schema_dictionary(version):
        if version == '1':
            return ProcessorParser.__schema_dict_v1

    @staticmethod
    def _check_valid_mode(input_category, input_name, keyword, valid_modes,
                          keyword_yaml):
        errors = []
        if keyword in keyword_yaml:
            mode = keyword_yaml[keyword]
            if mode not in valid_modes:
                valid_mode_str =\
                    ', '.join(["'" + x + "'" for x in valid_modes])
                errors.append(bad_mode.format(
                    input_category, input_name, keyword, mode, valid_mode_str))
        return errors

    @staticmethod
    def __check_resources_yaml_v1(input_category, input_name, resources_yaml):
        errors = []
        if 'resources' not in resources_yaml:
            errors.append(missing_field_named.format(
                input_category, input_name, 'resources'))

            for r, j in enumerate(resources_yaml['resources']):
                if 'varname' not in r:
                    errors.append(missing_resource_field_unnamed.format(
                        input_category, input_name, j, 'varname'))
                if 'resource' not in r:
                    errors.append(missing_resource_field_named.format(
                        input_category, input_name, r['varname'], 'resource'))
                if 'required' in r:
                    if r['required'] not in [True, False]:
                        errors.append(bad_resource_mode.format(
                            input_category, input_name, 'required'))
        return errors

    @staticmethod
    def __check_yaml_v1(log, yaml_source):

        # TODO: BenM/asr_of_asr/finish this!
        errors = []
        schema_number = yaml_source.get('yaml_processor_version', None)
        if schema_number != '0.1':
            errors.append('Error: Invalid schema num {}'.format(schema_number))

        if 'xnat' not in yaml_source:
            errors.append('Error: Missing xnat section')
        xnat_section = yaml_source['xnat']

        scan_section = xnat_section.get('scans', {})
        for s, i in enumerate(scan_section):
            if 'name' not in s:
                errors.append(missing_field_unnamed.format('scan', i, 'name'))
            name = s['name']

            if 'types' not in s:
                errors.append(
                    missing_field_named.format('scan', name, 'types'))

            errors.extend(
                ProcessorParser._check_valid_mode(
                    'scan', name, 'select', select_namespace, s))

            errors.extend(
                ProcessorParser._check_valid_mode(
                    'scan', name, 'select-session',
                    select_session_namespace, s))

            errors.extend(
                ProcessorParser.__check_resources_yaml_v1('scan', name, s))

        assr_section = xnat_section.get('assessors', {})
        for a, i in enumerate(assr_section):

            if 'name' not in a:
                errors.append(missing_field_unnamed.format('scan', i, 'name'))
            name = a['name']

            if 'types' not in a:
                errors.append(
                    missing_field_named.format('scan', name, 'types'))

            errors.extend(ProcessorParser._check_valid_mode(
                'assessor', name,
                'select', select_namespace, a))

            errors.extend(ProcessorParser._check_valid_mode(
                'assessor', name,
                'select-session', select_session_namespace, a))

            ProcessorParser.__check_resources_yaml_v1('assessor', name, a)

    @staticmethod
    def _get_args(statement):
        leftindex = statement.find('(')
        rightindex = statement.find(')')
        if leftindex == -1 and rightindex == -1:
            return [statement]
        elif leftindex != -1 and rightindex != -1:
            return [statement[:leftindex]] +\
                   [s.strip()
                    for s in statement[leftindex+1:rightindex].split(',')]
        else:
            raise ValueError('statement is malformed')

    @staticmethod
    def _parse_select(statement):
        if statement is None:
            statement = 'foreach'
        statement = statement.strip()
        return ProcessorParser._get_args(statement)

    @staticmethod
    def _parse_session_select(statement):
        if statement is None:
            statement = 'current'
        statement = statement.strip()
        args = ProcessorParser._get_args(statement)
        if args[0] == 'current':
            delta = 0
        elif args[0] in ['prior', 'prior-with']:
            delta = int(args[1])
        elif args[0] in ['first', 'first-with']:
            delta = sys.maxsize
        return SelectSessionParameters(args[0], delta)

    @staticmethod
    def _register_iteration_references(name, iteration_args, iteration_sources,
                                       iteration_map):
        if iteration_args[0] == 'foreach':
            if len(iteration_args) == 1:
                iteration_sources.add(name)
            else:
                iteration_map[name] = iteration_args[1]
        elif iteration_args[0] == 'one':
            iteration_sources.add(name)
        elif iteration_args[0] == 'all':
            iteration_sources.add(name)
        elif iteration_args[0] == 'from':
            iteration_map[name] = iteration_args[1]

    @staticmethod
    def _register_input_types(input_types, inputs_by_type, name):
        for t in input_types:
            ts = inputs_by_type.get(t, set())
            ts.add(name)
            inputs_by_type[t] = ts

    @staticmethod
    def _input_name(artefact):
        # candidates = list(filter(lambda v: v[1] is None, scan.iteritems()))
        # if len(candidates) != 1:
        #     raise ValueError(
        #         "invalid scan entry format; scan name cannot be determined")
        # return candidates[0][0]
        return artefact['name']

    @staticmethod
    def parse_inputs(yaml_source):
        # TODO: BenM/assessor_of_assessor/check error conditions on inputs:
        # . resource should be set
        # . no repeated input names
        # . ambiguous overlaps for input types (this may not be a problem)?

        inputs_by_type = {}
        iteration_sources = set()
        iteration_map = {}

        # get inputs: pass 1
        input_dict = yaml_source['inputs']
        xnat = input_dict['xnat']
        if xnat is None:
            raise ValueError(
                'yaml processor is missing xnat keyword contents')

        inputs = {}

        prior_session_count = 0

        # get scans
        scans = xnat.get('scans', list())
        for s in scans:
            name = ProcessorParser._input_name(s)

            select = s.get('select', None)
            parsed_select = ProcessorParser._parse_select(select)

            session_select = s.get('select-session', None)
            parsed_session_select =\
                ProcessorParser._parse_session_select(session_select)

            ProcessorParser._register_iteration_references(
                name,
                parsed_select,
                iteration_sources,
                iteration_map)

            types = [_.strip() for _ in s['types'].split(',')]
            ProcessorParser._register_input_types(types, inputs_by_type, name)

            resources = s.get('resources', [])
            artefact_required = False
            for r in resources:
                r['required'] = r.get('required', True)
                artefact_required = artefact_required or r['required']

            inputs[name] = {
                'types': types,
                'select': parsed_select,
                'select-session': parsed_session_select,
                'artefact_type': 'scan',
                'needs_qc': s.get('needs_qc', False),
                'resources': s.get('resources', []),
                'required': artefact_required
            }

            prior_session_count =\
                max(prior_session_count, parsed_session_select.delta)

        # get assessors
        asrs = xnat.get('assessors', list())
        for a in asrs:
            name = ProcessorParser._input_name(a)

            select = a.get('select', None)
            parsed_select = ProcessorParser._parse_select(select)

            session_select = a.get('select-session', None)
            parsed_session_select =\
                ProcessorParser._parse_session_select(session_select)

            ProcessorParser._register_iteration_references(
                name,
                parsed_select,
                iteration_sources,
                iteration_map)

            types = [_.strip() for _ in a['proctypes'].split(',')]
            ProcessorParser._register_input_types(types, inputs_by_type, name)

            resources = a.get('resources', [])
            artefact_required = False
            for r in resources:
                r['required'] = r.get('required', True)
            artefact_required = artefact_required or r['required']

            inputs[name] = {
                'types': types,
                'select': parsed_select,
                'select-session': parsed_session_select,
                'artefact_type': 'assessor',
                'needs_qc': a.get('needs_qc', False),
                'resources': a.get('resources', []),
                'required': artefact_required
            }

            prior_session_count =\
                max(prior_session_count, parsed_session_select.delta)

        return (inputs, inputs_by_type, iteration_sources, iteration_map,
                prior_session_count)

    @staticmethod
    def is_longitudinal(yaml_source):
        inputs = yaml_source['inputs']['xnat']

        entries = inputs.get('scans', list()) + inputs.get('assessors', list())

        for e in entries:
            if e.get('select-session', 'current') != 'current':
                return True
        return False

    @staticmethod
    def parse_match_filters(yaml_source):
        match_list = []
        try:
            _filters = yaml_source['inputs']['xnat']['filters']
        except KeyError:
            return []

        # Parse out filters, currently only filters of type match are supported
        for f in _filters:
            _type = f['type']
            if _type == 'match':
                # Split the comma-separated list of inputs
                _inputs = f['inputs'].split(',')
                match_list.append(_inputs)
            else:
                LOGGER.error('invalid filter type:{}'.format(_type))

        return match_list

    @staticmethod
    def parse_variables(inputs):
        variables_to_inputs = {}
        for ik, iv in list(inputs.items()):
            for r in iv['resources']:
                v = r.get('varname', '')
                if v is not None and len(v) > 0:
                    variables_to_inputs[v] =\
                        {'input': ik, 'resource': r['resource']}

        return variables_to_inputs

    @staticmethod
    def parse_artefacts(csesses):
        def parse(carts, arts):
            for cart in carts:
                resources = {}
                for cres in cart.resources():
                    resources[cres.label()] = cres
                full_path = cart.full_path()
                arts[full_path] = ParserArtefact(full_path,
                                                 resources,
                                                 cart)

        artefacts = {}
        for csess in csesses:
            parse(csess.scans(), artefacts)
            parse(csess.assessors(), artefacts)

        return artefacts

    @staticmethod
    def map_artefacts_to_inputs(csesses, inputs, inputs_by_type):

        artefacts_by_input = {k: [] for k in inputs}
        for i, iv in list(inputs.items()):
            if iv['select-session'].mode in ['prior', 'prior-with']:
                if iv['select-session'].delta >= len(csesses):
                    csess = None
                else:
                    csess = csesses[iv['select-session'].delta]
            elif iv['select-session'].mode in ['first', 'first-with']:
                csess = csesses[-1]
            else:
                csess = csesses[0]

            if csess is not None:
                for cscan in csess.scans():
                    for expression in iv['types']:
                        regex = utilities.extract_exp(expression)
                        if regex.match(cscan.type()):
                            if iv.get('select')[0] == 'all' and\
                                 cscan.info().get('quality') == 'unusable':
                                print('excluding unusable scan')
                            else:
                                artefacts_by_input[i].append(cscan.full_path())
                            # Break here so we don't match multiple times
                            break

                for cassr in csess.assessors():
                    if cassr.type() in iv['types']:
                        artefacts_by_input[i].append(cassr.full_path())

        return artefacts_by_input

    # TODO: BenM improve name of generate_parameter_matrix
    # TODO: BenM handle multiple args disallowed / allowed scenarios
    @staticmethod
    def generate_parameter_matrix(inputs,
                                  iteration_sources,
                                  iteration_map,
                                  artefacts,
                                  artefacts_by_input):
        # generate n dimensional input matrix based on iteration sources
        all_inputs = []
        input_dimension_map = []

        # check whether all inputs are present
        for i, iv in list(inputs.items()):
            if len(artefacts_by_input[i]) == 0 and iv['required'] is True:
                return []

        # add in None for optional inputs so that the matrix can be generated
        # without artefacts present for those inputs
        sanitised_inputs = {}
        for i, iv in list(inputs.items()):
            if len(artefacts_by_input[i]) == 0:
                sanitised_inputs[i] = [list().append(None)]
            else:
                sanitised_inputs[i] = artefacts_by_input[i]

        for i in iteration_sources:
            # find other inputs that map to this iteration source
            mapped_inputs = [i]
            select_fn = inputs[i]['select'][0]

            # first, check iteration source and get the appropriate list of
            # artefacts
            cur_input_vector = None
            if select_fn == 'foreach':
                cur_input_vector = sanitised_inputs[i][:]

            elif select_fn == 'all':
                cur_input_vector = [[sanitised_inputs[i][:]]]

            elif select_fn == 'some':
                input_count = min(len(sanitised_inputs[i]),
                                  inputs[i]['select'][1])
                cur_input_vector = [[sanitised_inputs[i][:input_count]]]

            elif select_fn == 'one':
                cur_input_vector = [sanitised_inputs[i][:1]]

            if select_fn in ['foreach', 'from']:
                # build up the set of mapped input vectors one by one based on
                # the select mode of the mapped input
                combined_input_vector = [cur_input_vector]
                for k, v in list(iteration_map.items()):
                    if inputs[k]['select'][0] == 'foreach':
                        (v1, v2) = v, None
                    else:
                        (v1, v2) = v.split('/')
                    if v1 == i:
                        mapped_inputs.append(k)
                        if inputs[k]['select'][0] == 'foreach':
                            combined_input_vector.append(
                                sanitised_inputs[k][:])
                        else:  # from
                            from_artefacts = sanitised_inputs[v1]
                            mapped_input_vector = []
                            for fa in from_artefacts:
                                a = artefacts[fa]
                                from_inputs = a.entity.get_inputs()
                                if from_inputs is not None:
                                    mapped_input_vector.append(
                                        from_inputs[v2.encode()].decode())

                            combined_input_vector.append(mapped_input_vector)

                    else:
                        pass

                # 'trim' the input vectors to the number of entries of the
                # shortest vector. We don't actually truncate the datasets but
                # just use the number when transposing, below
                min_entry_count = min((len(e) for e in combined_input_vector))

                # transpose from list of input vectors to input entry lists,
                # one per combination of inputs
                merged_input_vector = [
                    [None for col in range(len(combined_input_vector))]
                    for row in range(min_entry_count)]
                for row in range(min_entry_count):
                    for col in range(len(combined_input_vector)):
                        merged_input_vector[row][col] =\
                            combined_input_vector[col][row]

                all_inputs.append(mapped_inputs)
                input_dimension_map.append(merged_input_vector)

            else:
                all_inputs.append(mapped_inputs)
                input_dimension_map.append(cur_input_vector)

        # perform a cartesian product of the dimension map entries to get the
        # final input combinations
        matrix = [list(
            itertools.chain.from_iterable(x)) for x in itertools.product(
                *input_dimension_map)]

        matrix_headers = list(itertools.chain.from_iterable(all_inputs))

        # rebuild the matrix to order the inputs consistently
        final_matrix = []
        for r in matrix:
            row = dict()
            for i in range(len(matrix_headers)):
                row[matrix_headers[i]] = r[i]
            final_matrix.append(row)

        return final_matrix

    @staticmethod
    def compare_to_existing(csesses, proc_type, parameter_matrix):

        csess = csesses[0]

        assessors = [[] for _ in range(len(parameter_matrix))]

        for casr in [a for a in csess.assessors() if a.type() == proc_type]:
            inputs = casr.get_inputs()
            if inputs is None:
                LOGGER.warn('skipping, inputs field is empty:' + casr.label())
                return list()

            inputs = list(inputs.items())
            inputs = {key.decode(): val.decode() for key, val in inputs}
            for pi, p in enumerate(parameter_matrix):
                if inputs == p:
                    assessors[pi].append(casr)

        return list(zip(copy.deepcopy(parameter_matrix), assessors))

    @staticmethod
    def get_input_value(input_name, parameter, artefacts):
        if '/' not in input_name:
            # Matching on parent so keep this value
            _val = parameter[input_name]
        else:
            # Match is on a parent so parse out the parent/child
            (_parent_name, _child_name) = input_name.split('/')
            _parent_val = parameter[_parent_name]
            _parent_art = artefacts[_parent_val]

            # Get the inputs field from the child
            _parent_inputs = _parent_art.entity.get_inputs()
            _val = _parent_inputs[_child_name]

        return _val

    @staticmethod
    def filter_matrix(parameter_matrix, match_filters, artefacts):
        filtered_matrix = []
        for cur_param in parameter_matrix:
            # Reset matching for this param set
            all_match = True

            for cur_filter in match_filters:
                # Get the first value to compare with others
                first_val = ProcessorParser.get_input_value(
                    cur_filter[0], cur_param, artefacts)

                # Compare other values with first value
                for cur_input in cur_filter[1:]:
                    cur_val = ProcessorParser.get_input_value(
                        cur_input, cur_param, artefacts)

                    if cur_val != first_val:
                        # A single non-match breaks the whole thing
                        all_match = False
                        break

            if all_match:
                # Keep this param set if everything matches
                filtered_matrix.append(cur_param)

        return filtered_matrix
