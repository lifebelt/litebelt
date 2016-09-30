from __future__ import absolute_import

from django.conf import settings

from celery import shared_task

from classroom.models import AssignmentSubmission
from classroom.models import SubmissionReview
from app.models import GithubUser

import tempfile
import os.path

from git import Repo, GitCommandError
from github3 import login

GENADY_TOKEN = getattr(settings, 'GENADY_TOKEN', None)


@shared_task
def review_submission(submission_pk):

    gh = login(token=GENADY_TOKEN)

    submission = AssignmentSubmission.objects.get(pk=submission_pk)
    pull_request_number = submission.pull_request.split('/')[-1]
    repo = gh.repository(submission.pull_request.split('/')[-4], submission.pull_request.split('/')[-3])
    author = GithubUser.objects.get(github_id=gh.me().id)

    if author:
        desc = 'Compiled and running without problems!'

        review = SubmissionReview.objects.create(
            author=author, submission=submission, points=1, description=desc)

        course_dir = getattr(settings, 'GIT_ROOT', None)

        if not os.path.exists(course_dir):
            print("Cloning...")
            Repo.clone_from("https://github.com/lifebelt/litebelt-test", course_dir)

        r = Repo(course_dir)
        o = r.remotes.origin
        o.pull()

        pr = repo.pull_request(pull_request_number)

        with tempfile.NamedTemporaryFile() as temp:
            temp.write(pr.patch())
            temp.flush()
            try:

                r.git.checkout('HEAD', b='review#{}'.format(submission.id))
                r.git.apply('--ignore-space-change', '--ignore-whitespace', temp.name)
                r.git.checkout('master')
                r.git.checkout('.')
                r.git.branch(D='review#{}'.format(submission.id))

                # if everything is okay - merge and pull

            except GitCommandError as e:
                print(e)
                pr.create_comment("Git error while preparing to review...")

        pr.create_comment(desc)
