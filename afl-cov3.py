# afl-cov3 modified for python3

#
#  File: afl-cov3.py
#
#  Version: 0.6.2f (forked from 0.6.2)
#
#  Purpose: Perform lcov coverage diff's against each AFL queue file to see
#           new functions and line coverage evolve from an AFL fuzzing cycle.
#  Purpose (fork): Convert preexisting Python2 script to Python3
#
#  Copyright (C) 2015-2016 Michael Rash (mbr@cipherdyne.org)
#  Copyright (C) 2024-2025 XtremeBlaze777
#
#  License (GNU General Public License version 2 or any later version):
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02111-1301,
#  USA
#

from shutil import rmtree
from sys import argv
from tempfile import NamedTemporaryFile
import errno
import re
import glob
import string
import argparse
import time
import signal
import sys, os

try:
    import subprocess32 as subprocess
except ImportError:
    import subprocess

__version__ = '0.6.2f'

NO_OUTPUT   = 0
WANT_OUTPUT = 1
LOG_ERRORS  = 2

def main():

    exit_success = 0
    exit_failure = 1

    cargs = parse_cmdline()

    if cargs.version:
        print("afl-cov-" + __version__)
        return exit_success

    if cargs.gcov_check or cargs.gcov_check_bin:
        if is_gcov_enabled(cargs):
            return exit_success
        else:
            return exit_failure

    if not check_requirements(cargs):
        return exit_failure

    if cargs.stop_afl:
        return not stop_afl(cargs)

    if not validate_cargs(cargs):
        return exit_failure

    if cargs.validate_args:
        return exit_success

    if cargs.func_search or cargs.line_search:
        return not search_cov(cargs)

    if cargs.background:
        run_in_background()

    if cargs.live:
        is_afl_running(cargs)

    return not process_afl_test_cases(cargs)

def run_in_background():
    ### could use the python 'daemon' module, but it isn't always
    ### installed, and we just need a basic backgrounding
    ### capability anyway
    pid = os.fork()
    if (pid < 0):
        print("[*] fork() error, exiting.")
        os._exit()
    elif (pid > 0):
        os._exit(0)
    else:
        os.setsid()
    return

def process_afl_test_cases(cargs):

    rv        = True
    run_once  = False
    tot_files = 0
    fuzz_dir  = ''
    curr_file = ''

    afl_files = []
    cov_paths = {}

    ### main coverage tracking dictionary
    cov         = {}
    cov['zero'] = {}
    cov['pos']  = {}

    while True:

        if not import_fuzzing_dirs(cov_paths, cargs):
            rv = False
            break

        dir_ctr  = 0
        last_dir = False

        do_coverage = True
        if cargs.cover_corpus:
            do_coverage = False

        for fuzz_dir in cov_paths['dirs']:

            do_break  = False
            last_file = False
            num_files = 0
            new_files = []
            tmp_files = import_test_cases(fuzz_dir + '/queue')
            dir_ctr  += 1
            f_ctr     = 0

            if dir_ctr == len(cov_paths['dirs']):
                last_dir = True

            for f in tmp_files:
                if f not in afl_files:
                    afl_files.append(f)
                    new_files.append(f)

            if new_files:
                logr("\n*** Imported %d new test cases from: %s\n" \
                        % (len(new_files), (fuzz_dir + '/queue')),
                        cov_paths['log_file'], cargs)

            for f in new_files:

                f_ctr += 1
                if f_ctr == len(new_files):
                    last_file = True

                if cargs.cover_corpus and last_dir and last_file:
                    ### in --cover-corpus mode, only run lcov after all AFL
                    ### test cases have been processed
                    do_coverage = True

                out_lines = []
                curr_cycle = get_cycle_num(num_files, cargs)

                logr("[+] AFL test case: %s (%d / %d), cycle: %d" \
                        % (os.path.basename(f), num_files, len(afl_files),
                        curr_cycle), cov_paths['log_file'], cargs)

                cov_paths['diff'] = "%s/%s" % \
                        (cov_paths['diff_dir'], os.path.basename(f))
                id_range_update(f, cov_paths)

                ### execute the command to generate code coverage stats
                ### for the current AFL test case file
                if run_once:
                    run_cmd(cargs.coverage_cmd.replace('AFL_FILE', f),
                            cov_paths['log_file'], cargs, NO_OUTPUT)
                else:
                    out_lines = run_cmd(cargs.coverage_cmd.replace('AFL_FILE', f),
                            cov_paths['log_file'], cargs, WANT_OUTPUT)[1]
                    run_once = True

                if cargs.afl_queue_id_limit \
                        and num_files >= cargs.afl_queue_id_limit - 1:
                    logr("[+] queue/ id limit of %d reached..." \
                            % cargs.afl_queue_id_limit,
                            cov_paths['log_file'], cargs)
                    do_break = True
                    if cargs.cover_corpus and last_dir:
                        do_coverage = True

                if do_coverage and not cargs.coverage_at_exit:

                    ### generate the code coverage stats for this test case
                    lcov_gen_coverage(cov_paths, cargs)

                    ### diff to the previous code coverage, look for new
                    ### lines/functions, and write out results
                    coverage_diff(curr_cycle, fuzz_dir, cov_paths, f,
                            cov, cargs)

                    if cargs.cover_corpus:
                        ### reset the range values
                        cov_paths['id_min'] = cov_paths['id_max'] = -1

                    if cargs.lcov_web_all:
                        gen_web_cov_report(fuzz_dir, cov_paths, cargs)

                    ### log the output of the very first coverage command to
                    ### assist in troubleshooting
                    if len(out_lines):
                        logr("\n\n++++++ BEGIN - first exec output for CMD: %s" % \
                                (cargs.coverage_cmd.replace('AFL_FILE', f)),
                                cov_paths['log_file'], cargs)
                        for line in out_lines:
                            logr("    %s" % (line), cov_paths['log_file'], cargs)
                        logr("++++++ END\n", cov_paths['log_file'], cargs)

                cov_paths['id_file'] = "%s" % os.path.basename(f)

                num_files += 1
                tot_files += 1

                if do_break:
                    break

        if cargs.live:
            if is_afl_fuzz_running(cargs):
                if not len(new_files):
                    logr("[-] No new AFL test cases, sleeping for %d seconds" \
                            % cargs.sleep, cov_paths['log_file'], cargs)
                    time.sleep(cargs.sleep)
                    continue
            else:
                logr("[+] afl-fuzz appears to be stopped...",
                        cov_paths['log_file'], cargs)
                break
        ### only go once through the loop unless we are in --live mode
        else:
            break

    if tot_files > 0:
        logr("[+] Processed %d / %d test cases.\n" \
                % (tot_files, len(afl_files)),
                cov_paths['log_file'], cargs)

        if cargs.coverage_at_exit:
            ### generate the code coverage stats for this test case
            lcov_gen_coverage(cov_paths, cargs)

            ### diff to the previous code coverage, look for new
            ### lines/functions, and write out results
            coverage_diff(curr_cycle, fuzz_dir, cov_paths,
                    cov_paths['id_file'], cov, cargs)

        ### write out the final zero coverage and positive coverage reports
        write_zero_cov(cov['zero'], cov_paths, cargs)
        write_pos_cov(cov['pos'], cov_paths, cargs)

        if not cargs.disable_lcov_web:
            lcov_gen_coverage(cov_paths, cargs)
            gen_web_cov_report(fuzz_dir, cov_paths, cargs)

    else:
        if rv:
            logr("[*] Did not find any AFL test cases, exiting.\n",
                    cov_paths['log_file'], cargs)
        rv = False

    return rv

def id_range_update(afl_file, cov_paths):

    id_val = int(os.path.basename(afl_file).split(',')[0].split(':')[1])

    if cov_paths['id_min'] == -1:
        cov_paths['id_min'] = id_val
    elif id_val < cov_paths['id_min']:
        cov_paths['id_min'] = id_val

    if cov_paths['id_max'] == -1:
        cov_paths['id_max'] = id_val
    elif id_val > cov_paths['id_max']:
        cov_paths['id_max'] = id_val

    return

def coverage_diff(cycle_num, fuzz_dir, cov_paths, afl_file, cov, cargs):

    log_lines         = []
    delta_log_lines   = []
    print_diff_header = True

    ### defaults
    a_file = '(init)'
    if cov_paths['id_file']:
        a_file = cov_paths['id_file']
    delta_file = b_file = os.path.basename(afl_file)

    if cargs.cover_corpus or cargs.coverage_at_exit:
        a_file = 'id:%d...' % cov_paths['id_min']
        b_file = 'id:%d...' % cov_paths['id_max']
        delta_file = 'id:[%d-%d]...' % \
                (cov_paths['id_min'], cov_paths['id_max'])

    new_cov = extract_coverage(cov_paths['lcov_info_final'],
            cov_paths['log_file'], cargs)

    if not new_cov:
        return

    ### We aren't interested in the number of times AFL has executed
    ### a line or function (since we can't really get this anyway because
    ### gcov stats aren't influenced by AFL directly) - what we want is
    ### simply whether a new line or function has been executed at all by
    ### this test case. So, we look for new positive coverage.
    for f in new_cov['pos']:
        print_filename = True
        if f not in cov['zero'] and f not in cov['pos']: ### completely new file
            cov_init(f, cov)
            if print_diff_header:
                log_lines.append("diff %s -> %s" % \
                        (a_file, b_file))
                print_diff_header = False
            for ctype in new_cov['pos'][f]:
                for val in sorted(new_cov['pos'][f][ctype]):
                    cov['pos'][f][ctype][val] = ''
                    if print_filename:
                        log_lines.append("New src file: " + f)
                        print_filename = False
                    log_lines.append("  New '" + ctype + "' coverage: " + val)
                    if ctype == 'line':
                        if cargs.coverage_include_lines:
                            delta_log_lines.append("%s, %s, %s, %s, %s\n" \
                                    % (delta_file, cycle_num, f, ctype, val))
                    else:
                        delta_log_lines.append("%s, %s, %s, %s, %s\n" \
                                % (delta_file, cycle_num, f, ctype, val))
        elif f in cov['zero'] and f in cov['pos']:
            for ctype in new_cov['pos'][f]:
                for val in sorted(new_cov['pos'][f][ctype]):
                    if val not in cov['pos'][f][ctype]:
                        cov['pos'][f][ctype][val] = ''
                        if print_diff_header:
                            log_lines.append("diff %s -> %s" % \
                                    (a_file, b_file))
                            print_diff_header = False
                        if print_filename:
                            log_lines.append("Src file: " + f)
                            print_filename = False
                        log_lines.append("  New '" + ctype + "' coverage: " + val)
                        if ctype == 'line':
                            if cargs.coverage_include_lines:
                                delta_log_lines.append("%s, %s, %s, %s, %s\n" \
                                        % (delta_file, cycle_num, f, \
                                        ctype, val))
                        else:
                            delta_log_lines.append("%s, %s, %s, %s, %s\n" \
                                    % (delta_file, cycle_num, f, \
                                    ctype, val))

    ### now that new positive coverage has been added, reset zero
    ### coverage to the current new zero coverage
    cov['zero'] = {}
    cov['zero'] = new_cov['zero'].copy()

    if len(log_lines):
        logr("\n    Coverage diff %s %s" \
            % (a_file, b_file),
            cov_paths['log_file'], cargs)
        for l in log_lines:
            logr(l, cov_paths['log_file'], cargs)
            append_file(l, cov_paths['diff'])
        logr("", cov_paths['log_file'], cargs)

    if len(delta_log_lines):
        cfile = open(cov_paths['id_delta_cov'], 'a')
        for l in delta_log_lines:
            cfile.write(l)
        cfile.close()

    return

def write_zero_cov(zero_cov, cov_paths, cargs):

    cpath = cov_paths['zero_cov']

    logr("[+] Final zero coverage report: %s" % cpath,
            cov_paths['log_file'], cargs)
    cfile = open(cpath, 'w')
    cfile.write("# All functions / lines in this file were never executed by any\n")
    cfile.write("# AFL test case.\n")
    cfile.close()
    write_cov(cpath, zero_cov, cargs)
    return

def write_pos_cov(pos_cov, cov_paths, cargs):

    cpath = cov_paths['pos_cov']

    logr("[+] Final positive coverage report: %s" % cpath,
            cov_paths['log_file'], cargs)
    cfile = open(cpath, 'w')
    cfile.write("# All functions / lines in this file were executed by at\n")
    cfile.write("# least one AFL test case. See the cov/id-delta-cov file\n")
    cfile.write("# for more information.\n")
    cfile.close()
    write_cov(cpath, pos_cov, cargs)
    return

def write_cov(cpath, cov, cargs):
    cfile = open(cpath, 'a')
    for f in cov:
        cfile.write("File: %s\n" % f)
        for ctype in sorted(cov[f]):
            if ctype == 'function':
                for val in sorted(cov[f][ctype]):
                    cfile.write("    %s: %s\n" % (ctype, val))
            elif ctype == 'line':
                if cargs.coverage_include_lines:
                    for val in sorted(cov[f][ctype], key=int):
                        cfile.write("    %s: %s\n" % (ctype, val))
    cfile.close()

    return

def write_status(status_file):
    f = open(status_file, 'w')
    f.write("afl_cov_pid     : %d\n" % os.getpid())
    f.write("afl_cov_version : %s\n" % __version__)
    f.write("command_line    : %s\n" % ' '.join(argv))
    f.close()
    return

def append_file(pstr, path):
    f = open(path, 'a')
    f.write("%s\n" % pstr)
    f.close()
    return

def cov_init(cfile, cov):
    for k in ['zero', 'pos']:
        if k not in cov:
            cov[k] = {}
        if cfile not in cov[k]:
            cov[k][cfile] = {}
            cov[k][cfile]['function'] = {}
            cov[k][cfile]['line'] = {}
    return

def extract_coverage(lcov_file, log_file, cargs):

    search_rv = False
    tmp_cov = {}

    if not os.path.exists(lcov_file):
        logr("[-] Coverage file '%s' does not exist, skipping." % lcov_file,
                log_file, cargs)
        return tmp_cov

    ### populate old lcov output for functions/lines that were called
    ### zero times
    with open(lcov_file, 'rb') as f:
        current_file = ''
        for line in f:
            try: 
                line = line.decode('utf-8')
            except UnicodeDecodeError as decode_error:
                print(f'Warning:\n\t{decode_error}\nProceeding with execution')
                line = line.decode('utf-8', errors='ignore')

            line = line.strip()

            m = re.search(r'SF:(\S+)', line)
            if m and m.group(1):
                current_file = m.group(1)
                cov_init(current_file, tmp_cov)
                continue

            if current_file:
                m = re.search(r'^FNDA:(\d+),(\S+)', line)
                if m and m.group(2):
                    fcn = m.group(2) + '()'
                    if m.group(1) == '0':
                        ### the function was never called
                        tmp_cov['zero'][current_file]['function'][fcn] = ''
                    else:
                        tmp_cov['pos'][current_file]['function'][fcn] = ''
                    continue

                ### look for lines that were never called
                m = re.search(r'^DA:(\d+),(\d+)', line)
                if m and m.group(1):
                    lnum = m.group(1)
                    if m.group(2) == '0':
                        ### the line was never executed
                        tmp_cov['zero'][current_file]['line'][lnum] = ''
                    else:
                        tmp_cov['pos'][current_file]['line'][lnum] = ''

    return tmp_cov

def search_cov(cargs):

    search_rv = False

    id_delta_file = cargs.afl_fuzzing_dir + '/cov/id-delta-cov'
    log_file      = cargs.afl_fuzzing_dir + '/cov/afl-cov.log'

    with open(id_delta_file, 'rb') as f:
        for line in f:
            try:
                line = line.decode('utf-8')
            except UnicodeDecodeError as decode_error:
                print(f'Warning:\n\t{decode_error}\nProceeding with execution')
                line = line.decode('utf-8', errors='ignore')

            line = line.strip()
            ### id:NNNNNN*_file, cycle, src_file, cov_type, fcn/line\n")
            [id_file, cycle_num, src_file, cov_type, val] = line.split(', ')

            if cargs.func_search and cov_type == 'function' and val == cargs.func_search:
                if cargs.src_file:
                    if cargs.src_file == src_file:
                        logr("[+] Function '%s' in file: '%s' executed by: '%s', cycle: %s" \
                                % (val, src_file, id_file, cycle_num),
                                log_file, cargs)
                        search_rv = True
                else:
                    logr("[+] Function '%s' executed by: '%s', cycle: %s" \
                            % (val, id_file, cycle_num),
                            log_file, cargs)
                    search_rv = True

            if cargs.src_file == src_file \
                    and cargs.line_search and val == cargs.line_search:
                if cargs.src_file == src_file:
                    logr("[+] Line '%s' in file: '%s' executed by: '%s', cycle: %s" \
                            % (val, src_file, id_file, cycle_num),
                            log_file, cargs)
                    search_rv = True

    if not search_rv:
        if cargs.func_search:
            logr("[-] Function '%s' not found..." % cargs.func_search,
                    log_file, cargs)
        elif cargs.line_search:
            logr("[-] Line %s not found..." % cargs.line_search,
                    log_file, cargs)

    return search_rv

def get_cycle_num(id_num, cargs):

    ### default cycle
    cycle_num = 0

    if not is_dir(cargs.afl_fuzzing_dir + '/plot_data'):
        return cycle_num

    with open(cargs.afl_fuzzing_dir + '/plot_data') as f:
        for line in f:
            ### unix_time, cycles_done, cur_path, paths_total, pending_total,...
            ### 1427742641, 11, 54, 419, 45, 0, 2.70%, 0, 0, 9, 1645.47
            vals = line.split(', ')
            ### test the id number against the current path
            if vals[2] == str(id_num):
                cycle_num = int(vals[1])
                break

    return cycle_num

def lcov_gen_coverage(cov_paths, cargs):

    out_lines = []

    lcov_opts = ''
    if cargs.enable_branch_coverage:
        lcov_opts += ' --rc lcov_branch_coverage=1'
    if cargs.follow:
        lcov_opts += ' --follow'

    run_cmd(cargs.lcov_path \
            + lcov_opts
            + " --no-checksum --capture --directory " \
            + cargs.code_dir + " --output-file " \
            + cov_paths['lcov_info'], \
            cov_paths['log_file'], cargs, LOG_ERRORS)

    if (cargs.disable_lcov_exclude_pattern):
        out_lines = run_cmd(cargs.lcov_path \
                + lcov_opts
                + " --no-checksum -a " + cov_paths['lcov_base'] \
                + " -a " + cov_paths['lcov_info'] \
                + " --output-file " + cov_paths['lcov_info_final'], \
                cov_paths['log_file'], cargs, WANT_OUTPUT)[1]
    else:
        tmp_file = NamedTemporaryFile(delete=False)
        run_cmd(cargs.lcov_path \
                + lcov_opts
                + " --no-checksum -a " + cov_paths['lcov_base'] \
                + " -a " + cov_paths['lcov_info'] \
                + " --output-file " + tmp_file.name, \
                cov_paths['log_file'], cargs, LOG_ERRORS)
        out_lines = run_cmd(cargs.lcov_path \
                + lcov_opts
                + " --no-checksum -r " + tmp_file.name \
                + " " + cargs.lcov_exclude_pattern + "  --output-file " \
                + cov_paths['lcov_info_final'],
                cov_paths['log_file'], cargs, WANT_OUTPUT)[1]
        if os.path.exists(tmp_file.name):
            os.unlink(tmp_file.name)

    log_coverage(out_lines, cov_paths['log_file'], cargs)

    return

def log_coverage(out_lines, log_file, cargs):
    for line in out_lines:
        m = re.search(r'^\s+(lines\.\..*\:\s.*)', line)
        if m and m.group(1):
            logr("    " + m.group(1), log_file, cargs)
        else:
            m = re.search(r'^\s+(functions\.\..*\:\s.*)', line)
            if m and m.group(1):
                logr("    " + m.group(1), log_file, cargs)
            else:
                if cargs.enable_branch_coverage:
                    m = re.search(r'^\s+(branches\.\..*\:\s.*)', line)
                    if m and m.group(1):
                        logr("    " + m.group(1),
                                log_file, cargs)
    return

def gen_web_cov_report(fuzz_dir, cov_paths, cargs):

    genhtml_opts = ''

    if cargs.enable_branch_coverage:
        genhtml_opts += ' --branch-coverage'

    run_cmd(cargs.genhtml_path \
            + genhtml_opts
            + " --output-directory " \
            + cov_paths['web_dir'] + " " \
            + cov_paths['lcov_info_final'], \
            cov_paths['log_file'], cargs, LOG_ERRORS)

    logr("[+] Final lcov web report: %s/%s" % \
            (cov_paths['web_dir'], 'index.html'), cov_paths['log_file'], cargs)

    return

def is_afl_fuzz_running(cargs):

    pid = None
    stats_file = cargs.afl_fuzzing_dir + '/fuzzer_stats'

    if os.path.exists(stats_file):
        pid = get_running_pid(stats_file, r'fuzzer_pid\s+\:\s+(\d+)')
    else:
        for p in os.listdir(cargs.afl_fuzzing_dir):
            stats_file = "%s/%s/fuzzer_stats" % (cargs.afl_fuzzing_dir, p)
            if os.path.exists(stats_file):
                ### allow a single running AFL instance in parallel mode
                ### to mean that AFL is running (and may be generating
                ### new code coverage)
                pid = get_running_pid(stats_file, r'fuzzer_pid\s+\:\s+(\d+)')
                if pid:
                    break

    return pid

def get_running_pid(stats_file, pid_re):
    pid = None
    if not os.path.exists(stats_file):
        return pid
    with open(stats_file, 'rb') as f:
        for line in f:
            try:
                line = line.decode('utf-8')
            except UnicodeDecodeError as decode_error:
                print(f'Warning:\n\t{decode_error}\nProceeding with execution')
                line = line.decode('utf-8', errors='ignore')

            line = line.strip()
            ### fuzzer_pid     : 13238
            m = re.search(rpid_re, line)  # TODO: I suspect this is a typo and it should be pid_re
            if m and m.group(1):
                is_running = int(m.group(1))
                try:
                    os.kill(is_running, 0)
                except OSError as e:
                    if e.errno == errno.EPERM:
                        pid = is_running
                else:
                    pid = is_running
                break
    return pid

def run_cmd(cmd, log_file, cargs, collect):

    out = []

    if cargs.verbose:
        if log_file:
            logr("    CMD: %s" % cmd, log_file, cargs)
        else:
            print("    CMD: %s" % cmd)

    fh = None
    if cargs.disable_cmd_redirection or collect == WANT_OUTPUT \
            or collect == LOG_ERRORS:
        fh = NamedTemporaryFile(delete=False)
    else:
        fh = open(os.devnull, 'w')

    es = subprocess.call(cmd, stdin=None,
            stdout=fh, stderr=subprocess.STDOUT, shell=True)

    fh.close()

    if cargs.disable_cmd_redirection or collect == WANT_OUTPUT \
            or collect == LOG_ERRORS:
        with open(fh.name, 'rb') as f:
            for line in f:
                try:
                    decoded_line = line.decode('utf-8')
                except UnicodeDecodeError as decode_error:
                    print(f'Warning:\n\t{decode_error}\nProceeding with execution')
                    decoded_line = line.decode('utf-8', errors='ignore')
                out.append(decoded_line.rstrip('\n'))
        os.unlink(fh.name)

    if (es != 0) and (collect == LOG_ERRORS or collect == WANT_OUTPUT):
        if log_file:
            logr("    Non-zero exit status '%d' for CMD: %s" % (es, cmd),
                    log_file, cargs)
            for line in out:
                logr(line, log_file, cargs)
        else:
            print("    Non-zero exit status '%d' for CMD: %s" % (es, cmd))

    return es, out

def import_fuzzing_dirs(cov_paths, cargs):

    if not cargs.afl_fuzzing_dir:
        print("[*] Must specify AFL fuzzing dir with --afl-fuzzing-dir or -d")
        return False

    if 'top_dir' not in cov_paths:
        if not init_tracking(cov_paths, cargs):
            return False

    def_dir = cargs.afl_fuzzing_dir

    if is_dir("%s/queue" % def_dir):
        if def_dir not in cov_paths['dirs']:
            add_dir(def_dir, cov_paths)
    else:
        for p in os.listdir(def_dir):
            fuzz_dir = "%s/%s" % (def_dir, p)
            if is_dir(fuzz_dir):
                if is_dir("%s/queue" % fuzz_dir):
                    ### found an AFL fuzzing directory instance from
                    ### parallel AFL execution
                    if fuzz_dir not in cov_paths['dirs']:
                        add_dir(fuzz_dir, cov_paths)

    return True

def import_test_cases(qdir):
    return sorted(glob.glob(qdir + "/id:*"))

def init_tracking(cov_paths, cargs):

    cov_paths['dirs'] = {}

    cov_paths['top_dir']  = "%s/cov"  % cargs.afl_fuzzing_dir
    cov_paths['web_dir']  = "%s/web"  % cov_paths['top_dir']
    cov_paths['lcov_dir'] = "%s/lcov" % cov_paths['top_dir']
    cov_paths['diff_dir'] = "%s/diff" % cov_paths['top_dir']
    cov_paths['log_file'] = "%s/afl-cov.log" % cov_paths['top_dir']

    ### global coverage results
    cov_paths['id_delta_cov'] = "%s/id-delta-cov" % cov_paths['top_dir']
    cov_paths['zero_cov']     = "%s/zero-cov" % cov_paths['top_dir']
    cov_paths['pos_cov']      = "%s/pos-cov"  % cov_paths['top_dir']
    cov_paths['diff']         = ''
    cov_paths['id_file']      = ''
    cov_paths['id_min']       = -1  ### used in --cover-corpus mode
    cov_paths['id_max']       = -1  ### used in --cover-corpus mode

    ### raw lcov files
    cov_paths['lcov_base']       = "%s/trace.lcov_base" % cov_paths['lcov_dir']
    cov_paths['lcov_info']       = "%s/trace.lcov_info" % cov_paths['lcov_dir']
    cov_paths['lcov_info_final'] = "%s/trace.lcov_info_final" % cov_paths['lcov_dir']

    if cargs.overwrite:
        mkdirs(cov_paths, cargs)
    else:
        if is_dir(cov_paths['top_dir']):
            if not cargs.func_search and not cargs.line_search:
                print("[*] Existing coverage dir %s found, use --overwrite to " \
                        "re-calculate coverage" % (cov_paths['top_dir']))
                return False
        else:
            mkdirs(cov_paths, cargs)

    write_status("%s/afl-cov-status" % cov_paths['top_dir'])

    if not cargs.disable_coverage_init and cargs.coverage_cmd:

        lcov_opts = ''
        if cargs.enable_branch_coverage:
            lcov_opts += ' --rc lcov_branch_coverage=1 '

        ### reset code coverage counters - this is done only once as
        ### afl-cov is spinning up even if AFL is running in parallel mode
        run_cmd(cargs.lcov_path \
                + lcov_opts \
                + " --no-checksum --zerocounters --directory " \
                + cargs.code_dir, cov_paths['log_file'], cargs, LOG_ERRORS)

        run_cmd(cargs.lcov_path \
                + lcov_opts
                + " --no-checksum --capture --initial" \
                + " --directory " + cargs.code_dir \
                + " --output-file " \
                + cov_paths['lcov_base'], \
                cov_paths['log_file'], cargs, LOG_ERRORS)

    return True

### credit:
### http://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
def is_exe(fpath):
    return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

def is_bin_gcov_enabled(binary, cargs):

    rv = False

    ### run readelf against the binary to see if it contains gcov support
    for line in run_cmd("%s -a %s" % (cargs.readelf_path, binary),
            False, cargs, WANT_OUTPUT)[1]:
        if ' __gcov' in line:
            if cargs.validate_args or cargs.gcov_check or cargs.gcov_check_bin:
                print("[+] Binary '%s' is compiled with code coverage support via gcc." % binary)
            rv = True
            break

        if '__llvm_gcov' in line:
            if cargs.validate_args or cargs.gcov_check or cargs.gcov_check_bin:
                print("[+] Binary '%s' is compiled with code coverage support via llvm." % binary)
            rv = True
            break

    if not rv and cargs.gcov_check_bin:
        print("[*] Binary '%s' is not compiled with code coverage support." % binary)

    return rv

def which(prog):
    fpath, fname = os.path.split(prog)
    if fpath:
        if is_exe(prog):
            return prog
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            path = path.strip('"')
            exe_file = os.path.join(path, prog)
            if is_exe(exe_file):
                return exe_file
    return None

def check_requirements(cargs):
    lcov = which( "lcov" );
    gcov = which( "gcov" );
    genhtml = which( "genhtml" );

    if ( lcov == None ):
        lcov = which( cargs.lcov_path )
    if ( genhtml == None ):
        genhtml = which ( cargs.genhtml_path )

    if ( lcov == None or gcov == None):
        print("Required command not found :")
    else:
        if (genhtml == None and not cargs.disable_lcov_web):
            print("Required command not found :")
        else:
            return True

    if ( lcov == None ):
        print("[*] lcov command does not exist : %s" % (cargs.lcov_path))
    if ( genhtml == None and not cargs.disable_lcov_web):
        print("[*] genhtml command does not exist : %s" % (cargs.genhtml_path))
    if ( gcov == None ):
        print("[*] gcov command does not exist : %s" % (cargs.gcov_path))

    return False

def is_gcov_enabled(cargs):

    if not is_exe(cargs.readelf_path):
        print("[*] Need a valid path to readelf, use --readelf-path")
        return False

    if cargs.coverage_cmd:
        if 'AFL_FILE' not in cargs.coverage_cmd:
            print("[*] --coverage-cmd must contain AFL_FILE")
            return False

        ### make sure at least one component of the command is an
        ### executable and is compiled with code coverage support
        found_exec = False
        found_code_cov_binary = False

        for part in cargs.coverage_cmd.split(' '):
            if not part or part[0] == ' ' or part[0] == '-':
                continue
            if (which(part)):
                found_exec = True
                if not cargs.disable_gcov_check and is_bin_gcov_enabled(part, cargs):
                    found_code_cov_binary = True
                    break

        if not found_exec:
            print("[*] Could not find an executable binary " \
                    "--coverage-cmd '%s'" % cargs.coverage_cmd)
            return False

        if not cargs.disable_gcov_check and not found_code_cov_binary:
            print("[*] Could not find an executable binary with code " \
                    "coverage support ('-fprofile-arcs -ftest-coverage') " \
                    "in --coverage-cmd '%s'" % cargs.coverage_cmd)
            return False

    elif cargs.gcov_check_bin:
        if not is_bin_gcov_enabled(cargs.gcov_check_bin, cargs):
            return False
    elif cargs.gcov_check:
        print("[*] Either --coverage-cmd or --gcov-check-bin required in --gcov-check mode")
        return False

    return True

def validate_cargs(cargs):

    if cargs.coverage_cmd:
        if not is_gcov_enabled(cargs):
            return False
    else:
        if not cargs.func_search and not cargs.line_search:
            print("[*] Must set --coverage-cmd or --func-search/--line-search")
            return False

    if cargs.code_dir:
        if not is_dir(cargs.code_dir):
            print("[*] --code-dir path does not exist")
            return False

        ### make sure code coverage support is compiled in
        if not gcno_files_exist(cargs):
            return False

    else:
        if not cargs.func_search and not cargs.line_search:
            print("[*] Must set --code-dir unless using --func-search " \
                    "against existing afl-cov directory")
            return False

    if cargs.func_search or cargs.line_search:
        if not cargs.afl_fuzzing_dir:
            print("[*] Must set --afl-fuzzing-dir")
            return False
        if cargs.func_search and '()' not in cargs.func_search:
            cargs.func_search += '()'
        if cargs.line_search and not cargs.src_file:
            print("[*] Must set --src-file in --line-search mode")
            return False

    if cargs.live and not cargs.ignore_core_pattern:
        if not check_core_pattern():
            return False

    if not cargs.live and not is_dir(cargs.afl_fuzzing_dir):
        print("[*] It doesn't look like directory '%s' exists" \
            % (cargs.afl_fuzzing_dir))
        return False

    if cargs.disable_lcov_web and cargs.lcov_web_all:
        print("[*] --disable-lcov-web and --lcov-web-all are incompatible")
        return False

    return True


def gcno_files_exist(cargs):

    ### make sure the code has been compiled with code coverage support,
    ### so *.gcno files should exist
    found_code_coverage_support = False
    for root, dirs, files in os.walk(cargs.code_dir):
        for filename in files:
            if filename[-5:] == '.gcno':
                found_code_coverage_support = True
    if not found_code_coverage_support:
        print("[*] Could not find any *.gcno files in --code-dir " \
                "'%s', is code coverage ('-fprofile-arcs -ftest-coverage') " \
                "compiled in?" % cargs.code_dir)
        return False
    return True

def is_afl_running(cargs):
    while not is_dir(cargs.afl_fuzzing_dir):
        if not cargs.background:
            print("[-] Sleep for %d seconds for AFL fuzzing directory to be created..." \
                    % cargs.sleep)
        time.sleep(cargs.sleep)

    ### if we make it here then afl-fuzz is presumably running
    while not is_afl_fuzz_running(cargs):
        if not cargs.background:
            print("[-] Sleep for %d seconds waiting for afl-fuzz to be started...." \
                % cargs.sleep)
        time.sleep(cargs.sleep)
    return


def add_dir(fdir, cov_paths):
    cov_paths['dirs'][fdir] = {}
    return

def mkdirs(cov_paths, cargs):

    create_cov_dirs = False
    if is_dir(cov_paths['top_dir']):
        if cargs.overwrite:
            rmtree(cov_paths['top_dir'])
            create_cov_dirs = True
    else:
        create_cov_dirs = True

    if create_cov_dirs:
        for k in ['top_dir', 'web_dir', 'lcov_dir', 'diff_dir']:
            if not is_dir(cov_paths[k]):
                os.mkdir(cov_paths[k])

        ### write coverage results in the following format
        cfile = open(cov_paths['id_delta_cov'], 'w')
        if cargs.cover_corpus or cargs.coverage_at_exit:
            cfile.write("# id:[range]..., cycle, src_file, coverage_type, fcn/line\n")
        else:
            cfile.write("# id:NNNNNN*_file, cycle, src_file, coverage_type, fcn/line\n")
        cfile.close()

    return

def is_dir(dpath):
    return os.path.exists(dpath) and os.path.isdir(dpath)

def logr(pstr, log_file, cargs):
    if not cargs.background and not cargs.quiet:
        print("    " + pstr)
    append_file(pstr, log_file)
    return

def stop_afl(cargs):

    rv = True

    ### note that this function only looks for afl-fuzz processes - it does not
    ### stop afl-cov processes since they will stop on their own after afl-fuzz
    ### is also stopped.

    if not cargs.afl_fuzzing_dir:
        print("[*] Must set --afl-fuzzing-dir")
        return False

    if not is_dir(cargs.afl_fuzzing_dir):
        print("[*] Doesn't look like AFL fuzzing directory '%s' exists." \
                % cargs.afl_fuzzing_dir)
        return False

    if os.path.exists(cargs.afl_fuzzing_dir + '/fuzzer_stats'):
        afl_pid = get_running_pid(cargs.afl_fuzzing_dir + '/fuzzer_stats',
                r'fuzzer_pid\s+\:\s+(\d+)')
        if afl_pid:
            print("[+] Stopping running afl-fuzz instance, PID: %d" % afl_pid)
            os.kill(afl_pid, signal.SIGTERM)
        else:
            print("[-] No running afl-fuzz instance")
            rv = False
    else:
        found = False
        for p in os.listdir(cargs.afl_fuzzing_dir):
            stats_file = cargs.afl_fuzzing_dir + '/' + p + '/fuzzer_stats'
            if os.path.exists(stats_file):
                afl_pid = get_running_pid(stats_file, r'fuzzer_pid\s+\:\s+(\d+)')
                if afl_pid:
                    print("[+] Stopping running afl-fuzz instance, PID: %d" \
                            % afl_pid)
                    os.kill(afl_pid, signal.SIGTERM)
                    found = True
        if not found:
            print("[-] No running afl-fuzz instance")
            rv = False

    return rv

def check_core_pattern():

    rv = True

    core_pattern_file = '/proc/sys/kernel/core_pattern'

    ### check /proc/sys/kernel/core_pattern to see if afl-fuzz will
    ### accept it
    if os.path.exists(core_pattern_file):
        with open(core_pattern_file, 'r') as f:
            if f.readline().rstrip()[0] == '|':
                ### same logic as implemented by afl-fuzz itself
                print("[*] afl-fuzz requires 'echo core >%s'" \
                        % core_pattern_file)
                rv = False
    return rv

def parse_cmdline():

    p = argparse.ArgumentParser()

    p.add_argument("-e", "--coverage-cmd", type=str,
            help="Set command to exec (including args, and assumes code coverage support)")
    p.add_argument("-d", "--afl-fuzzing-dir", type=str,
            help="top level AFL fuzzing directory")
    p.add_argument("-c", "--code-dir", type=str,
            help="Directory where the code lives (compiled with code coverage support)")
    p.add_argument("-f", "--follow", action='store_true',
            help="Follow links when searching .da files", default=False)
    p.add_argument("-O", "--overwrite", action='store_true',
            help="Overwrite existing coverage results", default=False)
    p.add_argument("--disable-cmd-redirection", action='store_true',
            help="Disable redirection of command results to /dev/null",
            default=False)
    p.add_argument("--disable-lcov-web", action='store_true',
            help="Disable generation of all lcov web code coverage reports",
            default=False)
    p.add_argument("--disable-coverage-init", action='store_true',
            help="Disable initialization of code coverage counters at afl-cov startup",
            default=False)
    p.add_argument("--coverage-include-lines", action='store_true',
            help="Include lines in zero-coverage status files",
            default=False)
    p.add_argument("--enable-branch-coverage", action='store_true',
            help="Include branch coverage in code coverage reports (may be slow)",
            default=False)
    p.add_argument("--live", action='store_true',
            help="Process a live AFL directory, and afl-cov will exit when it appears afl-fuzz has been stopped",
            default=False)
    p.add_argument("--cover-corpus", action='store_true',
            help="Measure coverage after running all available tests instead of individually per queue file",
            default=False)
    p.add_argument("--coverage-at-exit", action='store_true',
            help="Only calculate coverage just before afl-cov exit.",
            default=False)
    p.add_argument("--sleep", type=int,
            help="In --live mode, # of seconds to sleep between checking for new queue files",
            default=60)
    p.add_argument("--gcov-check", action='store_true',
            help="Check to see if there is a binary in --coverage-cmd (or in --gcov-check-bin) has coverage support",
            default=False)
    p.add_argument("--gcov-check-bin", type=str,
            help="Test a specific binary for code coverage support",
            default=False)
    p.add_argument("--disable-gcov-check", type=str,
            help="Disable check for code coverage support",
            default=False)
    p.add_argument("--background", action='store_true',
            help="Background mode - if also in --live mode, will exit when the alf-fuzz process is finished",
            default=False)
    p.add_argument("--lcov-web-all", action='store_true',
            help="Generate lcov web reports for all id:NNNNNN* files instead of just the last one",
            default=False)
    p.add_argument("--disable-lcov-exclude-pattern", action='store_true',
            help="Allow default /usr/include/* pattern to be included in lcov results",
            default=False)
    p.add_argument("--lcov-exclude-pattern", type=str,
            help="Set exclude pattern for lcov results",
            default="/usr/include/*")
    p.add_argument("--func-search", type=str,
            help="Search for coverage of a specific function")
    p.add_argument("--line-search", type=str,
            help="Search for coverage of a specific line number (requires --src-file)")
    p.add_argument("--src-file", type=str,
            help="Restrict function or line search to a specific source file")
    p.add_argument("--afl-queue-id-limit", type=int,
            help="Limit the number of id:NNNNNN* files processed in the AFL queue/ directory",
            default=0)
    p.add_argument("--ignore-core-pattern", action='store_true',
            help="Ignore the /proc/sys/kernel/core_pattern setting in --live mode",
            default=False)
    p.add_argument("--lcov-path", type=str,
            help="Path to lcov command", default="/usr/bin/lcov")
    p.add_argument("--genhtml-path", type=str,
            help="Path to genhtml command", default="/usr/bin/genhtml")
    p.add_argument("--readelf-path", type=str,
            help="Path to readelf command", default="/usr/bin/readelf")
    p.add_argument("--stop-afl", action='store_true',
            help="Stop all running afl-fuzz instances associated with --afl-fuzzing-dir <dir>",
            default=False)
    p.add_argument("--validate-args", action='store_true',
            help="Validate args and exit", default=False)
    p.add_argument("-v", "--verbose", action='store_true',
            help="Verbose mode", default=False)
    p.add_argument("-V", "--version", action='store_true',
            help="Print version and exit", default=False)
    p.add_argument("-q", "--quiet", action='store_true',
            help="Quiet mode", default=False)

    return p.parse_args()

if __name__ == "__main__":
    sys.exit(main())
