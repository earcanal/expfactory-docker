#!/usr/bin/env python
# -*- coding: utf-8 -*-
from expdj.settings import DOMAIN_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY_ID
from expdj.apps.turk.utils import amazon_string_to_datetime, get_connection
from django.contrib.contenttypes.models import ContentType
from expdj.apps.experiments.models import Experiment, ExperimentTemplate, Battery
from boto.mturk.question import ExternalQuestion
from django.db.models.signals import pre_init
from django.contrib.auth.models import User
from django.db.models import Q, DO_NOTHING
from boto.mturk.price import Price
from jsonfield import JSONField
from django.db import models
import collections
import boto

def init_connection_callback(sender, **signal_args):
    """Mechanical Turk connection signal callback

    By using Django pre-init signals, class level connections can be
    made available to Django models that configure this pre-init
    signal.
    """
    sender.args = sender
    object_args = signal_args['kwargs']
    sender.connection = get_connection(AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY_ID)


class DisposeException(Exception):
    """Unable to Dispose of HIT Exception"""
    def __init__(self, value):
        self.parameter = value

    def __unicode__(self):
        return repr(self.parameter)
    __str__ = __unicode__

class Worker(models.Model):
    id = models.CharField(primary_key=True, max_length=200, null=False, blank=False)

    def __str__(self):
        return "%s" %(self.id)

    def __unicode__(self):
        return "%s" %(self.id)

    class Meta:
        ordering = ['id']





def get_worker(worker_id):
    # (<Worker: WORKER_ID: experiments[0]>, True)
    worker,_ = Worker.objects.update_or_create(id=worker_id)
    return worker

class HIT(models.Model):
    """An Amazon Mechanical Turk Human Intelligence Task as a Django Model"""

    def __str__(self):
        return "%s: %s" %(self.title,self.battery)

    def __unicode__(self):
        return "%s: %s" %(self.title,self.battery)


    (ASSIGNABLE, UNASSIGNABLE, REVIEWABLE, REVIEWING, DISPOSED) = (
          'A', 'U', 'R', 'G', 'D')

    (_ASSIGNABLE, _UNASSIGNABLE, _REVIEWABLE, _REVIEWING, _DISPOSED) = (
          "Assignable", "Unassignable", "Reviewable", "Reviewing", "Disposed")

    (NOT_REVIEWED, MARKED_FOR_REVIEW, REVIEWED_APPROPRIATE,
          REVIEWED_INAPPROPRIATE) = ("N", "M", "R", "I")

    (_NOT_REVIEWED, _MARKED_FOR_REVIEW, _REVIEWED_APPROPRIATE,
          _REVIEWED_INAPPROPRIATE) = ("NotReviewed", "MarkedForReview",
          "ReviewedAppropriate", "ReviewedInappropriate")

    STATUS_CHOICES = (
            (ASSIGNABLE, _ASSIGNABLE),
            (UNASSIGNABLE, _UNASSIGNABLE),
            (REVIEWABLE, _REVIEWABLE),
            (REVIEWING, _REVIEWING),
            (DISPOSED, _DISPOSED),
    )
    REVIEW_CHOICES = (
            (NOT_REVIEWED, _NOT_REVIEWED),
            (MARKED_FOR_REVIEW, _MARKED_FOR_REVIEW),
            (REVIEWED_APPROPRIATE, _REVIEWED_APPROPRIATE),
            (REVIEWED_INAPPROPRIATE, _REVIEWED_INAPPROPRIATE)
    )

    # Convenience lookup dictionaries for the above lists
    reverse_status_lookup = dict((v, k) for k, v in STATUS_CHOICES)
    reverse_review_lookup = dict((v, k) for k, v in REVIEW_CHOICES)

    # A HIT must be associated with a battery
    battery = models.ForeignKey(Battery, help_text="Battery of Experiments deployed by the HIT.", verbose_name="Experiment Battery", null=False, blank=False,on_delete=DO_NOTHING)
    owner = models.ForeignKey(User)
    mturk_id = models.CharField("HIT ID",max_length=255,unique=True,null=True,help_text="A unique identifier for the HIT")
    hit_type_id = models.CharField("HIT Type ID",max_length=255,null=True,blank=True,help_text="The ID of the HIT type of this HIT")
    creation_time = models.DateTimeField(null=True,blank=True,help_text="The UTC date and time the HIT was created")
    title = models.CharField(max_length=255,null=False,blank=False,help_text="The title of the HIT")
    description = models.TextField(null=False,blank=False,help_text="A general description of the HIT")
    keywords = models.TextField("Keywords",null=True,blank=True,help_text=("One or more words or phrases that describe the HIT, separated by commas."))
    status = models.CharField("HIT Status",max_length=1,choices=STATUS_CHOICES,null=True,blank=True,help_text="The status of the HIT and its assignments")
    reward = models.DecimalField(max_digits=5,decimal_places=3,null=False,blank=False,help_text=("The amount of money the requester will pay a worker for successfully completing the HIT"))
    lifetime_in_seconds = models.PositiveIntegerField(null=True,blank=True,help_text=("The amount of time, in seconds, after which the HIT is no longer available for users to accept."))
    assignment_duration_in_seconds = models.PositiveIntegerField(null=True,blank=True,help_text=("The length of time, in seconds, that a worker has to complete the HIT after accepting it."))
    max_assignments = models.PositiveIntegerField(null=True,blank=True,default=1,help_text=("The number of times the HIT can be accepted and  completed before the HIT becomes unavailable."))
    auto_approval_delay_in_seconds = models.PositiveIntegerField(null=True,blank=True,help_text=("The amount of time, in seconds after the worker submits an assignment for the HIT that the results are automatically approved by the requester."))
    requester_annotation = models.TextField(null=True,blank=True,help_text=("An arbitrary data field the Requester who created the HIT can use. This field is visible only to the creator of the HIT."))
    number_of_similar_hits = models.PositiveIntegerField(null=True,blank=True,help_text=("The number of HITs with fields identical to this HIT, other than the Question field."))
    review_status = models.CharField("HIT Review Status",max_length=1,choices=REVIEW_CHOICES,null=True,blank=True,help_text="Indicates the review status of the HIT.")
    number_of_assignments_pending = models.PositiveIntegerField(null=True,blank=True,help_text=("The number of assignments for this HIT that have been accepted by Workers, but have not yet been submitted, returned, abandoned."))
    number_of_assignments_available = models.PositiveIntegerField(null=True,blank=True,help_text=("The number of assignments for this HIT that are available for Workers to accept"))
    number_of_assignments_completed = models.PositiveIntegerField(null=True,blank=True,help_text=("The number of assignments for this HIT that have been approved or rejected."))

    def disable(self):
        """Disable/Destroy HIT that is no longer needed

        Remove a HIT from the Mechanical Turk marketplace, approves all
        submitted assignments that have not already been approved or
        rejected, and disposes of the HIT and all assignment data.

        Assignments for the HIT that have already been submitted, but
        not yet approved or rejected, will be automatically approved.
        Assignments in progress at the time of this method call  will be
        approved once the assignments are submitted. You will be charged
        for approval of these assignments. This method completely
        disposes of the HIT and all submitted assignment data.

        Assignment results data available at the time of this method
        call are saved in the Django models. However, additional
        Assignment results data cannot be retrieved for a HIT that has
        been disposed.

        It is not possible to re-enable a HIT once it has been disabled.
        To make the work from a disabled HIT available again, create a
        new HIT.

        This is a wrapper around the Boto API. Also see:
        http://boto.cloudhackers.com/en/latest/ref/mturk.html
        """
        # Check for new results and cache a copy in Django model
        self.update(do_update_assignments=True)
        self.connection.dispose_hit(self.mturk_id)

    def dispose(self):
        """Dispose of a HIT that is no longer needed.

        Only HITs in the "Reviewable" state, with all submitted
        assignments approved or rejected, can be disposed. This removes
        the data from Amazon Mechanical Turk, but not from the local
        Django database (i.e., a local cache copy is kept).

        This is a wrapper around the Boto API. Also see:
        http://boto.cloudhackers.com/en/latest/ref/mturk.html
        """

        # Don't waste time or resources if already marked as DISPOSED
        if self.status == self.DISPOSED:
            return

        # Check for new results and cache a copy in Django model
        self.update(do_update_assignments=True)

        # Verify HIT is reviewable
        if self.status != self.REVIEWABLE:
            #TODO: Excercise line
            raise DisposeException(
                    "Can't dispose of HIT (%s) that is still in %s status." % (
                        self.mturk_id,
                        dict(self.STATUS_CHOICES)[self.status]))

        # Verify Assignments are either APPROVED or REJECTED
        for assignment in self.assignments.all():
            if assignment.status not in [Assignment.APPROVED,
                    Assignment.REJECTED]:
                raise DisposeException(
                        "Can't dispose of HIT (%s) because Assignment "
                        "(%s) is not approved or rejected." % (
                            self.mturk_id, assignment.mturk_id))

        # Checks pass. Dispose of HIT and update status
        self.connection.dispose_hit(self.mturk_id)
        self.update()

    def expire(self):
        """Expire a HIT that is no longer needed as Mechanical Turk service

        The effect is identical to the HIT expiring on its own. The HIT
        no longer appears on the Mechanical Turk web site, and no new
        Workers are allowed to accept the HIT. Workers who have accepted
        the HIT prior to expiration are allowed to complete it or return
        it, or allow the assignment duration to elapse (abandon the HIT).
        Once all remaining assignments have been submitted, the expired
        HIT moves to the "Reviewable" state.

        This is a thin wrapper around the Boto API and taken from their
        documentation:
        http://boto.cloudhackers.com/en/latest/ref/mturk.html
        """
        if not self.has_connection():
            self.generate_connection()
        self.connection.expire_hit(self.mturk_id)
        self.update()

    def extend(self, assignments_increment=None, expiration_increment=None):
        """Increase the maximum assignments or extend the expiration date

        Increase the maximum number of assignments, or extend the
        expiration date, of an existing HIT.

        NOTE: If a HIT has a status of Reviewable and the HIT is
        extended to make it Available, the HIT will not be returned by
        helpers.update_reviewable_hits() and its submitted assignments
        will not be returned by Assignment.update() until the HIT is
        Reviewable again. Assignment auto-approval will still happen on
        its original schedule, even if the HIT has been extended. Be
        sure to retrieve and approve (or reject) submitted assignments
        before extending the HIT, if so desired.

        This is a thin wrapper around the Boto API and taken from their
        documentation:
        http://boto.cloudhackers.com/en/latest/ref/mturk.html
        """
        self.connection.extend_hit(self.mturk_id,
                                   assignments_increment=assignments_increment,
                                   expiration_increment=expiration_increment)
        self.update()

    def set_reviewing(self, revert=None):
        """Toggle HIT status between Reviewable and Reviewing

        Update a HIT with a status of Reviewable to have a status of
        Reviewing, or reverts a Reviewing HIT back to the Reviewable
        status.

        Only HITs with a status of Reviewable can be updated with a
        status of Reviewing. Similarly, only Reviewing HITs can be
        reverted back to a status of Reviewable.

        This is a thin wrapper around the Boto API and taken from their
        documentation:
        http://boto.cloudhackers.com/en/latest/ref/mturk.html
        """
        self.connection.set_reviewing(self.mturk_id, revert=revert)
        self.update()

    def generate_connection(self):
        self.connection = get_connection(AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY_ID)
        self.save()

    def has_connection(self):
        if "turk.HIT.connection" in [x.__str__() for x in self._meta.fields]:
            return True
        return False

    def send_hit(self):

        # Domain name must be https
        url = "%s/turk/%s" %(DOMAIN_NAME,self.id)
        frame_height = 900
        questionform = ExternalQuestion(url, frame_height)

        result = self.connection.create_hit(
                title=self.title,
                description=self.description,
                keywords=self.keywords,
                max_assignments=self.max_assignments,
                question=questionform,
                reward=Price(amount=self.reward),
                response_groups=('Minimal', 'HITDetail'),  # I don't know what response groups are
        )[0]
        # Update our hit object with the aws HIT
        self.mturk_id = result.HITId

        # When we generate the hit, we won't have any assignments to update
        self.update(mturk_hit=result)

    def save(self, *args, **kwargs):
        '''save will generate a connection and get
        the mturk_id for hits that have not been saved yet
        '''
        is_new_hit = False
        send_to_mturk = False
        if not self.pk:
            is_new_hit = True
        if not self.mturk_id:
            send_to_mturk = True
        super(HIT, self).save(*args, **kwargs)
        if is_new_hit:
            self.generate_connection()
        if send_to_mturk:
            self.send_hit()

    def update(self, mturk_hit=None, do_update_assignments=False):
        """Update self with Mechanical Turk API data

        If mturk_hit is given to this function, it should be a Boto
        hit object that represents a Mechanical Turk HIT instance.
        Otherwise, Amazon Mechanical Turk is contacted to get additional
        information.

        This instance's attributes are updated.
        """
        self.generate_connection()
        if mturk_hit is None or not hasattr(mturk_hit, "HITStatus"):
            hit = self.connection.get_hit(self.mturk_id)[0]
        else:
            assert isinstance(mturk_hit, boto.mturk.connection.HIT)
            hit = mturk_hit

        self.status = HIT.reverse_status_lookup[hit.HITStatus]
        self.reward = hit.Amount
        self.assignment_duration_in_seconds = hit.AssignmentDurationInSeconds
        self.auto_approval_delay_in_seconds = hit.AutoApprovalDelayInSeconds
        self.max_assignments = hit.MaxAssignments
        self.creation_time = amazon_string_to_datetime(hit.CreationTime)
        self.description = hit.Description
        self.title = hit.Title
        self.hit_type_id = hit.HITTypeId
        self.keywords = hit.Keywords
        if hasattr(self, 'NumberOfAssignmentsCompleted'):
            self.number_of_assignments_completed = hit.NumberOfAssignmentsCompleted
        if hasattr(self, 'NumberOfAssignmentsAvailable'):
            self.number_of_assignments_available = hit.NumberOfAssignmentsAvailable
        if hasattr(self, 'NumberOfAssignmentsPending'):
            self.number_of_assignments_pending = hit.NumberOfAssignmentsPending
        #'CurrencyCode', 'Reward', 'Expiration', 'expired']

        self.save()

        if do_update_assignments:
            self.update_assignments()

    def update_assignments(self, page_number=1, page_size=10, update_all=True):
        self.generate_connection()
        assignments = self.connection.get_assignments(self.mturk_id,
                                                      page_size=page_size,
                                                      page_number=page_number)
        for mturk_assignment in assignments:
            assert mturk_assignment.HITId == self.mturk_id
            djurk_assignment = Assignment.objects.get_or_create(
                    mturk_id=mturk_assignment.AssignmentId, hit=self)[0]
            djurk_assignment.update(mturk_assignment, hit=self)
        if update_all and int(assignments.PageNumber) *\
                            page_size < int(assignments.TotalNumResults):
            self.update_assignments(page_number + 1, page_size, update_all)

    class Meta:
        verbose_name = "HIT"
        verbose_name_plural = "HITs"

    def __unicode__(self):
        return u"HIT: %s" % self.mturk_id


class Assignment(models.Model):
    '''An Amazon Mechanical Turk Assignment'''

    (_SUBMITTED, _APPROVED, _REJECTED) = ("Submitted", "Approved", "Rejected")
    (SUBMITTED, APPROVED, REJECTED) = ("S", "A", "R")

    STATUS_CHOICES = (
            (SUBMITTED, _SUBMITTED),
            (APPROVED, _APPROVED),
            (REJECTED, _REJECTED),
    )
    # Convenience lookup dictionaries for the above lists
    reverse_status_lookup = dict((v, k) for k, v in STATUS_CHOICES)

    mturk_id = models.CharField("Assignment ID",max_length=255,unique=True,null=True,help_text="A unique identifier for the assignment")
    worker = models.ForeignKey(Worker,null=True,blank=True,help_text="The ID of the Worker who accepted the HIT")
    hit = models.ForeignKey(HIT,null=True,blank=True,related_name='assignments')
    status = models.CharField(max_length=1,choices=STATUS_CHOICES,null=True,blank=True,help_text="The status of the assignment")
    auto_approval_time = models.DateTimeField(null=True,blank=True,help_text=("If results have been submitted, this is the date and time, in UTC,  the results of the assignment are considered approved automatically if they have not already been explicitly approved or rejected by the requester"))
    accept_time = models.DateTimeField(null=True,blank=True,help_text=("The date and time, in UTC, the Worker accepted the assignment"))
    submit_time = models.DateTimeField(null=True,blank=True,help_text=("If the Worker has submitted results, this is the date and time, in UTC, the assignment was submitted"))
    approval_time = models.DateTimeField(null=True,blank=True,help_text=("If requester has approved the results, this is the date and time, in UTC, the results were approved"))
    rejection_time = models.DateTimeField(null=True,blank=True,help_text=("If requester has rejected the results, this is the date and time, in UTC, the results were rejected"))
    deadline = models.DateTimeField(null=True,blank=True,help_text=("The date and time, in UTC, of the deadline for the assignment"))
    requester_feedback = models.TextField(null=True,blank=True,help_text=("The optional text included with the call to either approve or reject the assignment."))
    completed = models.BooleanField(choices=((False, 'Not completed'),
                                             (True, 'Completed')),
                                              default=False,verbose_name="participant completed the entire assignment")


    def create(self):
        init_connection_callback(sender=self.hit)

    def approve(self, feedback=None):
        """Thin wrapper around Boto approve function."""
        self.hit.generate_connection()
        self.hit.connection.approve_assignment(self.mturk_id, feedback=feedback)
        self.update()

    def reject(self, feedback=None):
        """Thin wrapper around Boto reject function."""
        self.hit.generate_connection()
        self.hit.connection.reject_assignment(self.mturk_id, feedback=feedback)
        self.update()

    def bonus(self, value=0.0, feedback=None):
        """Thin wrapper around Boto bonus function."""
        self.hit.generate_connection()

        self.hit.connection.grant_bonus(
                self.worker_id,
                self.mturk_id,
                bonus_price=boto.mturk.price.Price(amount=value),
                reason=feedback)
        self.update()

    def update(self, mturk_assignment=None, hit=None):
        """Update self with Mechanical Turk API data

        If mturk_assignment is given to this function, it should be
        a Boto assignment object that represents a Mechanical Turk
        Assignment instance.  Otherwise, Amazon Mechanical Turk is
        contacted.

        This instance's attributes are updated.
        """
        self.hit.generate_connection()
        assignment = None

        if mturk_assignment is None:
            hit = self.hit.connection.get_hit(self.hit.mturk_id)[0]
            for a in self.hit.connection.get_assignments(hit.HITId):
                # While we have the query, we may as well update
                if a.AssignmentId == self.mturk_id:
                    # That's this record. Hold onto so we can update below
                    assignment = a
                else:
                    other_assignment = Assignment.objects.get(
                            mturk_id=a.AssignmentId)
                    other_assignment.update(a)
        else:
            assert isinstance(mturk_assignment,
                              boto.mturk.connection.Assignment)
            assignment = mturk_assignment

        if assignment != None:
            self.status = self.reverse_status_lookup[assignment.AssignmentStatus]
            self.worker_id = get_worker(assignment.WorkerId)
            self.submit_time = amazon_string_to_datetime(assignment.SubmitTime)
            self.accept_time = amazon_string_to_datetime(assignment.AcceptTime)
            self.auto_approval_time = amazon_string_to_datetime(assignment.AutoApprovalTime)
            self.submit_time = amazon_string_to_datetime(assignment.SubmitTime)

            # Different response groups for query
            if hasattr(assignment, 'RejectionTime'):
                self.rejection_time = amazon_string_to_datetime(assignment.RejectionTime)
            if hasattr(assignment, 'ApprovalTime'):
                self.approval_time = amazon_string_to_datetime(assignment.ApprovalTime)

            # Update any Key-Value Pairs that were associated with this
            # assignment
            for result_set in assignment.answers:
                for question in result_set:
                    for key, value in question.fields:
                        kv = KeyValue.objects.get_or_create(key=key,assignment=self)[0]
                        if kv.value != value:
                            kv.value = value
                            kv.save()
        self.save()

    def __unicode__(self):
        return self.mturk_id

    def __repr__(self):
        return u"Assignment: %s" % self.mturk_id
    __str__ = __unicode__


class Result(models.Model):
    '''A result holds a battery id and an experiment template, to keep track of the battery/experiment combinations that a worker has completed'''
    taskdata = JSONField(null=True,blank=True,load_kwargs={'object_pairs_hook': collections.OrderedDict})
    worker = models.ForeignKey(Worker,null=False,blank=False,related_name='result_worker')
    experiment = models.ForeignKey(ExperimentTemplate,help_text="The Experiment Template completed by the worker in the battery",null=False,blank=False,on_delete=DO_NOTHING)
    battery = models.ForeignKey(Battery, help_text="Battery of Experiments deployed by the HIT.", verbose_name="Experiment Battery", null=False, blank=False,on_delete=DO_NOTHING)
    assignment = models.ForeignKey(Assignment,null=False,blank=True,related_name='assignment')
    datetime = models.CharField(max_length=128,null=True,blank=True,help_text="DateTime string returned by browser at last submission of data")
    current_trial = models.PositiveIntegerField(null=True,blank=True,help_text=("The last (current) trial recorded as complete represented in the results."))
    language = models.CharField(max_length=128,null=True,blank=True,help_text="language of the browser associated with the result")
    browser = models.CharField(max_length=128,null=True,blank=True,help_text="browser of the result")
    platform = models.CharField(max_length=128,null=True,blank=True,help_text="platform of the result")
    completed = models.BooleanField(choices=((False, 'Not completed'),
                                             (True, 'Completed')),
                                              default=False,verbose_name="participant completed the experiment")
    credit_granted = models.BooleanField(choices=((False, 'Not granted'),
                                                  (True, 'Granted')),
                                                  default=False,verbose_name="the function assign_experiment_credit has been run to allocate credit for this result")

    class Meta:
        verbose_name = "Result"
        verbose_name_plural = "Results"
        unique_together = ("worker","assignment","battery","experiment")

    def __repr__(self):
        return u"Result: id[%s],worker[%s],battery[%s],experiment[%s]" %(self.id,self.worker,self.battery,self.experiment)

    def __unicode__(self):
        return u"Result: id[%s],worker[%s],battery[%s],experiment[%s]" %(self.id,self.worker,self.battery,self.experiment)


class KeyValue(models.Model):
    """Answer/Key Value Pairs"""

    MAX_DISPLAY_LENGTH = 255

    key = models.CharField(
            max_length=255,
            help_text="The Key (variable) for a QuestionAnswer"
    )
    value = models.TextField(
            null=True,
            blank=True,
            help_text="The value associated with the key",
    )
    assignment = models.ForeignKey(
            Assignment,
            null=True,
            blank=True,
            related_name="answers",
    )

    def short_value(self):
        if len(self.value) > self.MAX_DISPLAY_LENGTH:
            return u'%s...' % self.value[:self.MAX_DISPLAY_LENGTH]
        else:
            return self.value
    short_value.short_description = "Value (%d chars)..." % MAX_DISPLAY_LENGTH

    class Meta:
        verbose_name = "Key-Value Pair"
        verbose_name_plural = "Key-Value Pairs"

    def __unicode__(self):
        return u"%s=%s" % (self.key, self.short_value())
