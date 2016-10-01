from __future__ import absolute_import

from django.conf import settings

from celery import shared_task

from classroom.models import GithubUser, Student, AssignmentTask, AssignmentSubmission

from django.db.models import Sum

import tempfile
import os.path
from os import path, walk
import re
import shlex
import math
from enum import Enum
from subprocess import Popen, PIPE, TimeoutExpired

from git import Repo, GitCommandError
from github3 import login

GENADY_TOKEN = getattr(settings, 'GENADY_TOKEN', None)

TESTCASE_TIMEOUT = 1
GCC_TEMPLATE = 'gcc -Wall -std=c11 -pedantic {0} -o {1} -lm 2>&1'
FILENAME_TEMPLATES = ('.*task(\d+)\.[cC]$', '(\d\d+)_.*\.[cC]$')


class TaskStatus(Enum):
    SUBMITTED = 1
    UNSUBMITTED = 0


class ExecutionStatus(Enum):
    MISMATCH = 1
    TIMEOUT = 2
    OTHER = 3


@shared_task
def review_submission(submission_pk):

    gh = login(token=GENADY_TOKEN)
    course_dir = os.path.join(getattr(settings, 'GIT_ROOT', None), str(submission_pk))

    submission = AssignmentSubmission.objects.get(pk=submission_pk)
    author = GithubUser.objects.get(github_id=gh.me().id)

    if not author:
        return

    api, repo, pull = initialize_repo(submission, course_dir, gh)

    student = Student.objects.get(user__github_id=pull.user.id)
    if not student:
        pull.create_comment('User not recognized as student, calling the police!')
        pull.close()
        pass

    working_dir = os.path.join(course_dir, '{}/{}/{}/'.format(
                               student.student_class,
                               submission.assignment.assignment_index,
                               str(student.student_number).zfill(2)))

    with tempfile.NamedTemporaryFile() as temp:
        temp.write(pull.patch())
        temp.flush()
        try:
            # Create working branch and apply the pull-request patch on it
            repo.git.checkout('HEAD', b='review#{}'.format(submission.id))
            repo.git.am('--ignore-space-change', '--ignore-whitespace', temp.name)

            files = []
            for root, _, filenames in walk(working_dir, topdown=False):
                files += [
                    (f, path.abspath(path.join(working_dir, f)))
                    for f
                    in filenames
                    if (path.isfile(path.join(root, f)) and
                        (f.endswith('.c') or f.endswith('.C')))
                ]

            # if everything is okay - merge and pull
            summary = []
            completed_tasks = []
            unrecognized_files = []

            tasks = AssignmentTask.objects.filter(assignment=submission.assignment)
            tasks_count = len(tasks)
            tasks_points = tasks.aggregate(Sum('points'))

            for current, abs_path in files:
                task_index = get_task_number_from_filename(current)

                if (task_index is None or
                        task_index > tasks_count or
                        task_index <= 0):
                    unrecognized_files.append({
                        'name': current
                    })
                    continue

                selected = tasks.get(number=task_index)

                completed_tasks.append(task_index)
                task = {}
                task['name'] = selected.title
                task['index'] = task_index
                task['points'] = selected.points

                compiled_name = current.split('.')[0] + '.out'
                exec_path = os.path.abspath(
                    os.path.join(course_dir, compiled_name))

                gcc_invoke = GCC_TEMPLATE.format(shlex.quote(abs_path),
                                                 shlex.quote(exec_path))

                out, err, code = execute(gcc_invoke, timeout=10)
                msg = out + err

                if code != 0:
                    summary.append({
                        'status': TaskStatus.SUBMITTED,
                        'compiled': False,
                        'compiler_exit_code': code,
                        'compiler_message': remove_path_from_output(
                            os.path.abspath(course_dir), msg.decode()),
                        'task': task
                    })
                    continue

                testcases = []
                for test in selected.testcases.all():
                    try:
                        (stdout, stderr, exitcode) = \
                            execute(exec_path,
                                    input=test.case_input.encode('utf-8'))
                    except (FileNotFoundError, IOError, Exception):
                        testcases.append({
                            "index": test.id,
                            "success": False,
                            "status": ExecutionStatus.OTHER,
                        })
                        continue

                    output = stdout.decode('latin-1') or ""
                    output = " ".join(
                        filter(None, [line.strip() for line in output.split('\n')]))
                    if exitcode != 0:
                        testcases.append({
                            "index": test.id,
                            "success": False,
                            "status": ExecutionStatus.TIMEOUT,
                            "input": test.case_input,
                        })
                        continue

                    if output == test.case_output:
                        testcases.append({
                            "index": test.id,
                            "success": True
                        })
                    else:
                        testcases.append({
                            "index": test.id,
                            "success": False,
                            "status": ExecutionStatus.MISMATCH,
                            "input": test.case_input,
                            "output": output,
                            "expected": test.case_output,
                        })

                summary.append({
                    "status": TaskStatus.SUBMITTED,
                    "compiled": True,
                    "task": task,
                    "testcases": testcases,
                    "compiler_message": remove_path_from_output(
                        os.path.abspath(course_dir), msg.decode())
                })

            # Report for unsubmitted tasks
            for unsubmitted in tasks.exclude(number__in=completed_tasks):
                task = {}
                task['name'] = unsubmitted.title
                task['index'] = unsubmitted.number
                task['points'] = unsubmitted.points
                summary.append({
                    'status': TaskStatus.UNSUBMITTED,
                    'compiled': False,
                    'task': task
                })

            publish_result(summary, unrecognized_files, pull, tasks_points)

        except GitCommandError as e:
            print(e)
            pull.create_comment('I have some troubles!')
        finally:
            try:
                print('Cleanup...')
                # Checkout master, clear repo state and delete work branch
                repo.git.checkout('master')
                repo.git.checkout('.')
                repo.git.clean('-fd')
                repo.git.branch(D='review#{}'.format(submission.id))
            except GitCommandError as e:
                print(e)


def clone_repo_if_needed(directory):
    if not os.path.exists(directory):
        print('Cloning...')
        Repo.clone_from('https://github.com/lifebelt/litebelt-test', directory)


def initialize_repo(submission, directory, login):
    clone_repo_if_needed(directory)

    pull_request_number = submission.pull_request.split('/')[-1]

    api = login.repository(submission.pull_request.split('/')[-4], submission.pull_request.split('/')[-3])
    pr = api.pull_request(pull_request_number)

    repo = Repo(directory)
    o = repo.remotes.origin
    o.pull()

    return (api, repo, pr)


def is_valid_taskname(filename):
    for regexp_str in FILENAME_TEMPLATES:
        match = re.match(regexp_str, filename, flags=0)
        if match:
            return True

    return False


def get_task_number_from_filename(filename):
    for regexp_str in FILENAME_TEMPLATES:
        match = re.match(regexp_str, filename, flags=0)
        if match:
            return int(match.group(1))
    return None


def remove_path_from_output(folder, output):
    return output.replace(folder + os.sep, '')


def execute(command, input=None, timeout=1):
    proc = Popen(command, shell=True, stdout=PIPE, stderr=PIPE)

    try:
        std_out, std_err = proc.communicate(timeout=timeout, input=input)
    except TimeoutExpired:
        proc.kill()
        std_out, std_err = proc.communicate()

    return (std_out, std_err, proc.returncode)


def publish_result(summary, unrecognized, pull, points):
    sb = []
    for task in sorted(summary, key=lambda x: x['task']['index']):
        task_ = task["task"]
        sb.append(
            "## Task {}: {} [{}/{} points]\n".format(
                task_["index"],
                task_["name"],
                get_points_for_task(task),
                task_["points"]))

        if task["status"] is TaskStatus.UNSUBMITTED:
            sb.append("### Not submitted\n")
            continue

        if not task["compiled"]:
            sb.append("Failed compiling\n")
            sb.append("Exit code: {}\n".format(task["compiler_exit_code"]))
            sb.append("Error\n")
            sb.append("```\n{}\n```\n".format(task["compiler_message"]))
            continue

        if task["compiler_message"]:
            print("Compiled with warning(s)\n")
            sb.append("```\n{}\n```\n".format(task["compiler_message"]))

        for testcase in task["testcases"]:
            sb.append("### Testcase {}\n".format(testcase["index"]))

            if testcase["success"]:
                sb.append("passed\n")
                continue

            sb.append("failed\n")
            if testcase["status"] is ExecutionStatus.MISMATCH:
                sb.append("Input:\n")
                sb.append("```\n{}\n```\n\n".format(testcase["input"]))
                sb.append("Expected:\n")
                sb.append("```\n{}\n```\n\n".format(testcase["expected"]))
                sb.append("Output:\n")
                sb.append("```\n{}\n```\n\n".format(testcase["output"]))
            elif testcase["status"] is ExecutionStatus.TIMEOUT:
                sb.append("Execution took more than {} seconds\n".format(TESTCASE_TIMEOUT))

    if len(unrecognized) > 0:
        sb.append('## Unrecognized files')
        sb.append('\n')

        for unrecognized in sorted(unrecognized, key=lambda x: x['name']):
            sb.append('- {}'.format(unrecognized['name']))

    earned_points = get_earned_points(summary)

    sb.append('\n\n')
    sb.append('## Overall\n\n')
    sb.append('### Points earned: **{}** of max **{}**\n'.format(
              earned_points, points['points__sum']))

    pull.create_comment(''.join(sb))

    if (get_earned_points(summary) == points['points__sum'] and not pull.is_merged() and pull.mergeable):
        pull.merge()


def get_total_points(summary):
    return sum(map(lambda x: x['task']['points'], summary))


def get_points_for_task(task):
    if "testcases" not in task:
        return 0
    correct_tc = sum(testcase["success"] for testcase in task["testcases"])

    points = task['task']['points'] * \
        float(correct_tc) / len(task["testcases"])

    if task["compiler_message"]:
        points -= correct_tc

    return math.ceil(points)


def get_earned_points(summary):
    result = 0
    for task in summary:
        if task.get("testcases") is None:
            continue

        result += get_points_for_task(task)
    return result
