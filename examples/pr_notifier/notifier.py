import json
import logging
import os
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, ClassVar, Literal, Tuple

import boto3
from dataclass_wizard import JSONWizard
from slack_sdk import WebClient
from slack_sdk.models import blocks as blks

from scw_serverless.app import Serverless

SCW_ACCESS_KEY = os.environ["SCW_ACCESS_KEY"]
SCW_SECRET_KEY = os.environ["SCW_SECRET_KEY"]
S3_BUCKET = os.environ["S3_BUCKET"]
SLACK_TOKEN = os.environ["SLACK_TOKEN"]
SLACK_CHANNEL = os.environ["SLACK_CHANNEL"]
GITLAB_EMAIL_DOMAIN = os.getenv("GITLAB_EMAIL_DOMAIN")
REMINDER_SCHEDULE = os.getenv("REMINDER_SCHEDULE", "0 9 * * 1-5")

app = Serverless(
    "slack-bots",
    env={
        "S3_BUCKET": S3_BUCKET,
        "SLACK_CHANNEL": SLACK_CHANNEL,
        "GITLAB_EMAIL_DOMAIN": GITLAB_EMAIL_DOMAIN,
        "PYTHONUNBUFFERED": "1",
    },
    secret={
        "SLACK_TOKEN": SLACK_TOKEN,
        "SCW_ACCESS_KEY": SCW_ACCESS_KEY,
        "SCW_SECRET_KEY": SCW_SECRET_KEY,
    },
)

s3 = boto3.resource(
    "s3",
    region_name="fr-par",
    use_ssl=True,
    endpoint_url="https://s3.fr-par.scw.cloud",
    aws_access_key_id=SCW_ACCESS_KEY,
    aws_secret_access_key=SCW_SECRET_KEY,
)

# Enable info logging
logging.basicConfig(
    format="%(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    level=logging.INFO,
)
client = WebClient(token=SLACK_TOKEN)


@dataclass
class Developer(JSONWizard):
    """Generic representation of a user from GitHub/GitLab."""

    name: str
    avatar_url: str | None
    email: str | None = None

    @staticmethod
    def from_github(user: dict[str, Any]):
        """Creates from a GitHub user"""
        return Developer(name=user["login"], email=None, avatar_url=user["avatar_url"])

    @staticmethod
    def from_gitlab(user: dict[str, Any]):
        """Creates from a GitLab user"""
        email = user["username"] + GITLAB_EMAIL_DOMAIN if GITLAB_EMAIL_DOMAIN else ""
        return Developer(
            name=user["username"], email=email, avatar_url=user["avatar_url"]
        )

    def get_slack_username(self) -> str:
        """Gets the name that should be used on Slack."""
        if not self.email:
            return self.name

        response = client.users_lookupByEmail(email=self.email)
        if not response["ok"]:
            logging.error("Getting slack id for %s: %s", self.name, response["error"])
            return self.name

        return f'<@{response["user"]["id"]}>'  # type: ignore


@dataclass
class Review(JSONWizard):
    """Generic representation of a review from GitHub/GitLab."""

    state: Literal["approved", "dismissed", "changes_requested", "left_note"]
    _slack_emojis: ClassVar[dict[str, str]] = {
        "approved": ":heavy_check_mark:",
        "dismissed": ":put_litter_in_its_place:",
        "changes_requested": ":x:",
        "left_note": ":x:",
    }
    _slack_message: ClassVar[dict[str, str]] = {
        "approved": "approved the pull request",
        "dismissed": "dismissed the pull request",
        "changes_requested": "requested some changes",
        "left_note": "left a comment",
    }

    @staticmethod
    def from_github(review: dict[str, Any]):
        """Creates from a GitHub review."""
        return Review(state=review["state"].lower())

    @staticmethod
    def from_gitlab_action(
        action: Literal["approval", "approved", "unapproval", "unapproved"],
    ):
        """Creates from a GitLab action."""
        return Review(
            state="approved" if action.startswith("approv") else "changes_requested"
        )

    @staticmethod
    def from_gitlab_note():
        """Creates from a GitLab note.

        This is different than an actual review but it's the only event
        sent by GitLab when someone requests some changes.
        """
        return Review(state="left_note")

    @property
    def slack_emoji(self) -> str:
        """Gets the corresponding slack emoji."""
        return self._slack_emojis.get(self.state, "")

    @property
    def slack_message(self) -> str:
        """Gets the corresponding slack message."""
        return self._slack_message.get(self.state, "")


@dataclass
class Repository(JSONWizard):
    """Generic representation of a GitHub/GitLab repository."""

    name: str
    full_name: str

    @staticmethod
    def from_github(repository: dict[str, Any]):
        """Creates from a GitHub repository."""
        return Repository(name=repository["name"], full_name=repository["full_name"])

    @staticmethod
    def from_gitlab(repository: dict[str, Any]):
        """Creates from a GitLab project."""
        return Repository(
            name=repository["name"], full_name=repository["path_with_namespace"]
        )


# pylint: disable=too-many-instance-attributes # unecessary work to split the attributes
@dataclass
class PullRequest(JSONWizard):
    """Generic representation of a GitHub PR/Gitlab MR."""

    number: int
    repository: Repository
    title: str
    url: str
    is_draft: bool
    is_merged: bool
    owner: Developer
    reviewers: dict[str, Developer]  # key is the Developer name property
    reviews: dict[str, Review]
    target_branch: str
    mergeable: bool | None
    additions: int | None
    deletions: int | None

    @property
    def bucket_path(self) -> str:
        """Get the path to store this PR in."""
        return f"pull_requests/{self.repository.full_name}/{self.number}.json"

    # pylint: disable=line-too-long # disabled to include links to documentation
    @staticmethod
    def from_github(repository: dict[str, Any], pull_request: dict[str, Any]):
        """Creates from a GitHub PR.

        .. seealso::

            GitHub Documentation
            https://docs.github.com/en/rest/pulls/pulls?apiVersion=2022-11-28#get-a-pull-request
        """
        return PullRequest(
            number=pull_request["number"],
            repository=Repository.from_github(repository),
            title=pull_request["title"],
            url=pull_request["html_url"],
            is_draft=pull_request["draft"],
            is_merged=pull_request.get("merged", False),  # not available in reviews
            owner=Developer.from_github(pull_request["user"]),
            reviewers={
                d["login"]: Developer.from_github(d)
                for d in pull_request["requested_reviewers"]
            },
            reviews={},
            target_branch=pull_request["base"]["ref"],
            mergeable=pull_request.get("mergeable"),
            additions=pull_request.get("additions"),
            deletions=pull_request.get("deletions"),
        )

    @staticmethod
    def from_gitlab(
        project: dict[str, Any],
        pull_request: dict[str, Any],
        user: dict[str, Any],
        reviewers: list[dict[str, Any]],
    ):
        """Creates from a GitLab MR event."""
        return PullRequest(
            number=pull_request["id"],
            repository=Repository.from_gitlab(project),
            title=pull_request["title"],
            url=pull_request["url"],
            is_draft=pull_request["work_in_progress"],
            is_merged=("action" in pull_request and pull_request["action"] == "merge"),
            owner=Developer.from_gitlab(user),  # only true when action is create
            reviewers={d["username"]: Developer.from_gitlab(d) for d in reviewers},
            reviews={},
            target_branch=pull_request["target_branch"],
            mergeable=(pull_request.get("detailed_merge_status") == "mergeable"),
            additions=None,
            deletions=None,
        )

    def on_draft(self) -> None:
        """Saves a PR marked as a draft to notify when it's ready."""
        save_pr_to_bucket(self, "")

    def on_created(self) -> None:
        """Sends a notification for a newly created PR."""
        response = client.chat_postMessage(
            channel=SLACK_CHANNEL, blocks=self._as_slack_notification()
        )
        if not response["ok"]:
            logging.error(
                "Sending created message for #%s in %s: %s",
                self.number,
                self.repository,
                response["error"],
            )
            return
        timestamp = str(response["ts"])
        save_pr_to_bucket(self, timestamp)

    def on_updated(self) -> None:
        """Performs the necessary changes when a PR is updated."""
        try:
            _, pull = load_pr_from_bucket(self.bucket_path)
        except s3.meta.client.exceptions.NoSuchKey:
            logging.warning(
                "Pull request #%s in %s not found",
                self.number,
                self.repository.full_name,
            )
            return
        if pull.is_draft and not self.is_draft:
            self.on_created()

    def on_reviewed(self, review: Review, reviewer: Developer) -> None:
        """Updates the notification when a new review is made."""
        try:
            timestamp, pull = load_pr_from_bucket(self.bucket_path)
        except s3.meta.client.exceptions.NoSuchKey:
            logging.warning(
                "Pull request #%s in %s not found",
                self.number,
                self.repository.full_name,
            )
            return

        self.reviews = pull.reviews.copy()
        if review.state == "left_note" and reviewer.name in self.reviews:
            # Ignore note "reviews" if there is an actual review
            # Also avoids spam when someone leaves multiple comments
            logging.info(
                "User %s left a note on a reviewed PR #%s in %s",
                reviewer.name,
                self.number,
                self.repository.full_name,
            )
            return

        self.reviews[reviewer.name] = review
        # On GitLab the owner is not sent on review events
        # We extract the owner from what's been saved
        self.owner = pull.owner
        if review.state == "left_note":
            # Reviewer block is not sent on note events
            self.reviewers = pull.reviewers

        # Prevents self-reviews from triggering a notification
        if self.owner.name == reviewer.name:
            logging.info(
                "User %s reviewed their own PR #%s in %s",
                reviewer.name,
                self.number,
                self.repository.full_name,
            )
            return

        save_pr_to_bucket(self, timestamp)

        response = client.chat_update(
            channel=SLACK_CHANNEL, ts=timestamp, blocks=self._as_slack_notification()
        )
        if not response["ok"]:
            logging.error(
                "Updating review message for #%s in %s: %s",
                self.number,
                self.repository.full_name,
                response["error"],
            )

        response = client.chat_postMessage(
            channel=SLACK_CHANNEL,
            thread_ts=timestamp,
            text=f"{reviewer.name} {review.slack_message}",
        )
        if not response["ok"]:
            logging.warning(
                "Sending review notification for #%s in %s: %s",
                self.number,
                self.repository.full_name,
                response["error"],
            )

    def on_closed(self) -> None:
        """Sends a message in the thread when the PR is merged."""
        if self.is_merged:
            timestamp, _pull = load_pr_from_bucket(self.bucket_path)
            response = client.chat_postMessage(
                channel=SLACK_CHANNEL,
                thread_ts=timestamp,
                text="Pull request was merged! :tada:",
            )
            if not response["ok"]:
                logging.error(
                    "Sending merge notification for #%s in %s: %s",
                    self.number,
                    self.repository.full_name,
                    response["error"],
                )

        delete_pr_from_bucket(self.bucket_path)

    def _as_slack_notification(self) -> list[blks.Block]:
        return [
            blks.HeaderBlock(text=f"New MR on {self.repository.name}: {self.title}"),
            blks.DividerBlock(),
            blks.ContextBlock(
                elements=[
                    blks.MarkdownTextObject(text="Submitted by"),
                    blks.ImageElement(
                        image_url=self.owner.avatar_url,
                        alt_text=f"avatar of {self.owner.name}",
                    ),
                    blks.MarkdownTextObject(text=f"*{self.owner.name}*"),
                ]
            ),
            blks.SectionBlock(
                fields=[self._get_details_slack_blk(), self._get_reviews_slack_blk()],
                accessory=blks.ButtonElement(text="Review", url=self.url),
            ),
        ]

    def _get_details_slack_blk(self) -> blks.MarkdownTextObject:
        txt = "*Details*\n"
        txt += f"Target: *{self.target_branch}*\n"
        txt += "Mergeable: "
        txt += ":ok_hand:\n" if self.mergeable else ":no_good:\n"
        if self.additions:
            txt += f"Additions: *{self.additions}*\n"
        if self.deletions:
            txt += f"Deletions: *{self.deletions}*\n"
        return blks.MarkdownTextObject(text=txt)

    def _get_reviews_slack_blk(self) -> blks.MarkdownTextObject:
        txt = "*Reviews*\n"
        for name, reviewer in self.reviewers.items():
            txt += reviewer.get_slack_username()
            if review := self.reviews.get(name):
                txt += ": " + review.slack_emoji
            txt += "\n"
        for name, review in self.reviews.items():
            if name not in self.reviewers:
                txt += f"{name}: " + review.slack_emoji + "\n"
        return blks.MarkdownTextObject(text=txt)

    def reminder_message(self) -> str | None:
        """Gets the message to add in the reminder.

        Returns None if the PR should not appear in the reminder.
        """
        if self.is_draft:
            return None
        if self.mergeable:
            return f"Mergeable: {self.owner.get_slack_username()}"

        missing_reviewers = [
            reviewer.get_slack_username()
            for name, reviewer in self.reviewers.items()
            if name not in self.reviews
        ]
        if not missing_reviewers:
            return None

        return f'Missing reviews: {", ".join(missing_reviewers)}'

    def get_reminder_slack_blk(self, reminder_message: str) -> blks.SectionBlock:
        """Gets the message to be added to the reminder."""
        reminder = f"*<{self.url}|{self.title}>* {reminder_message}"
        return blks.SectionBlock(
            text=blks.MarkdownTextObject(text=reminder, verbatim=True),
        )


def delete_pr_from_bucket(bucket_path: str) -> None:
    """Deletes a PR."""
    s3.Object(S3_BUCKET, bucket_path).delete()


def save_pr_to_bucket(pull: PullRequest, timestamp: str) -> None:
    """Saves a PR associated with a Slack timestamp."""
    s3.Object(S3_BUCKET, pull.bucket_path).put(
        Body=json.dumps({"ts": timestamp, "pull_request": pull.to_dict()})
    )


def load_pr_from_bucket(bucket_path: str) -> Tuple[str, PullRequest]:
    """Loads a PR and the Slack timestamp of its notification."""
    saved = json.loads(
        s3.Object(S3_BUCKET, bucket_path).get()["Body"].read().decode("utf-8")
    )
    return (saved["ts"], PullRequest.from_dict(saved["pull_request"]))


@app.func(description="GitHub webhook to notify on new PRs")
def handle_github(event: dict[str, Any], _content: dict[str, Any]) -> dict[str, Any]:
    """Handles GitHub webhook request.

    .. seealso::

        GitHub Events Documentation
        https://docs.github.com/en/developers/webhooks-and-events/webhooks/webhook-events-and-payloads#pull_request
    """
    body = json.loads(event["body"])
    match body:
        case {
            "zen": _,  # GitHub trivia included only on ping events
            "repository": repository,
        }:
            logging.info(
                "Hook is now active on repository: %s", repository["full_name"]
            )
            return {"statusCode": HTTPStatus.OK}
        case {
            "action": "opened" | "reopened",
            "pull_request": pull_request,
            "repository": repository,
        }:
            pull = PullRequest.from_github(repository, pull_request)
            if pull.is_draft:
                pull.on_draft()
            else:
                pull.on_created()
        case {
            "review": review,
            "sender": reviewer,
            "pull_request": pull_request,
            "repository": repository,
        }:
            pull = PullRequest.from_github(repository, pull_request)
            review = Review.from_github(review)
            reviewer = Developer.from_github(reviewer)
            pull.on_reviewed(review, reviewer)
        case {
            "action": "closed",
            "pull_request": pull_request,
            "repository": repository,
        }:
            pull = PullRequest.from_github(repository, pull_request)
            pull.on_closed()
        case _:
            logging.warning("Action %s is not supported", body.get("action"))
            return {"statusCode": HTTPStatus.BAD_REQUEST}
    return {"statusCode": HTTPStatus.OK}


@app.func(
    min_scale=1, memory_limit=1024, description="GitLab webhook to notify on new MRs"
)
def handle_gitlab(event: dict[str, Any], _content: dict[str, Any]) -> dict[str, Any]:
    """Handles GitLab webhook request.

    .. seealso::

        GitLab Events Documentation
        https://docs.gitlab.com/ee/user/project/integrations/webhook_events.html#merge-request-events

        GitLab Webhook Guidelines
        https://docs.gitlab.com/ee/user/project/integrations/webhooks.html
    """
    body = json.loads(event["body"])
    match body:
        case {
            "event_type": "merge_request",
            "user": user,
            "project": project,
            "object_attributes": {"action": "open" | "reopen" | "update"},
        }:
            pull_request = body["object_attributes"]
            reviewers = body.get("reviewers", [])
            pull = PullRequest.from_gitlab(project, pull_request, user, reviewers)
            if pull_request["action"] == "update":
                pull.on_updated()
            elif pull.is_draft:
                pull.on_draft()
            else:
                pull.on_created()
        case {
            "event_type": "merge_request",
            "user": user,
            "project": project,
            "object_attributes": {
                "action": "approval" | "approved" | "unapproval" | "unapproved"
            },
        }:
            pull_request = body["object_attributes"]
            reviewers = body.get("reviewers", [])
            pull = PullRequest.from_gitlab(project, pull_request, user, reviewers)
            pull.on_reviewed(
                Review.from_gitlab_action(pull_request["action"]), pull.owner
            )
        case {
            "event_type": "merge_request",
            "user": user,
            "project": project,
            "object_attributes": {"action": "merge" | "close"},
        }:
            pull_request = body["object_attributes"]
            pull = PullRequest.from_gitlab(project, pull_request, user, [])
            pull.on_closed()
        case {
            "event_type": "note",
            "user": user,
            "project": project,
            "merge_request": pull_request,
            "object_attributes": _,  # The note object itself
        }:
            pull = PullRequest.from_gitlab(project, pull_request, user, [])
            pull.on_reviewed(Review.from_gitlab_note(), pull.owner)
        case _:
            logging.warning("Event %s is not supported", body.get("event_type"))
    return {"statusCode": HTTPStatus.OK}


@app.schedule(REMINDER_SCHEDULE)
def pull_request_reminder(
    _event: dict[str, Any], _content: dict[str, Any]
) -> dict[str, Any]:
    """Daily reminder to review opened pull-requests."""
    blocks = [blks.HeaderBlock(text="PRs awaiting for review: "), blks.DividerBlock()]
    for opened_pr in s3.Bucket(S3_BUCKET).objects.all():
        _, pull = load_pr_from_bucket(opened_pr.key)
        if message := pull.reminder_message():
            logging.info(
                "Pull request #%s in %s is waiting for review",
                pull.number,
                pull.repository.full_name,
            )
            blocks.append(pull.get_reminder_slack_blk(message))
        else:
            logging.info(
                "Pull request #%s on %s was not included in the reminder",
                pull.number,
                pull.repository.full_name,
            )

    if len(blocks) <= 2:
        logging.info("No pull request was included in reminder")
        return {"statusCode": HTTPStatus.OK}

    response = client.chat_postMessage(channel=SLACK_CHANNEL, blocks=blocks)
    if not response["ok"]:
        logging.error(
            "Sending daily reminder: %s",
            response["error"],
        )
        return {"statusCode": HTTPStatus.INTERNAL_SERVER_ERROR}
    return {"statusCode": HTTPStatus.OK}
