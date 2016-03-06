import random
import string
import time
from datetime import datetime
from datetime import timedelta
from subprocess import check_call
from subprocess import PIPE
from subprocess import Popen

import pkg_resources
import pymysql
import pytest
from freezegun import freeze_time

from ocflib.printing.quota import add_job
from ocflib.printing.quota import add_refund
from ocflib.printing.quota import daily_quota
from ocflib.printing.quota import get_quota
from ocflib.printing.quota import Job
from ocflib.printing.quota import Refund
from ocflib.printing.quota import SEMESTERLY_QUOTA
from ocflib.printing.quota import UserQuota
from ocflib.printing.quota import WEEKDAY_QUOTA
from ocflib.printing.quota import WEEKEND_QUOTA


MYSQL_TIMEOUT = 10

TODAY = datetime.today()
YESTERDAY = TODAY - timedelta(days=1)
LAST_SEMESTER = TODAY - timedelta(days=365)

TEST_JOB = Job(
    user='nobody',
    time=datetime.now(),
    pages=3,
    queue='double',
    printer='logjam',
    doc_name='asdf',
    filesize=12,
)
TEST_REFUND = Refund(
    user='nobody',
    time=datetime.now(),
    pages=3,
    staffer='ckuehl',
    reason='just because',
)


@pytest.mark.parametrize('time,expected', [
    ('2015-08-22', WEEKEND_QUOTA),  # Saturday
    ('2015-08-23', WEEKEND_QUOTA),  # Sunday
    ('2015-08-24', WEEKDAY_QUOTA),  # Monday
    ('2015-08-25', WEEKDAY_QUOTA),  # Tuesday
    ('2015-08-26', WEEKDAY_QUOTA),  # Wednesday
])
def test_daily_quota(time, expected):
    """Test that the daily quota returns reasonable things."""
    time = datetime.strptime(time, '%Y-%m-%d')
    with freeze_time(time):
        assert daily_quota() == expected
    assert daily_quota(time) == expected


def test_quotas_are_sane():
    assert SEMESTERLY_QUOTA > 0
    assert WEEKDAY_QUOTA > 0
    assert WEEKEND_QUOTA > WEEKDAY_QUOTA
    assert WEEKDAY_QUOTA < SEMESTERLY_QUOTA
    assert WEEKEND_QUOTA < SEMESTERLY_QUOTA


def assert_quota(c, user, diff_daily, diff_semesterly):
    """Assert the quota for a user is what we expect.

    Typically, you want to pass a negative number for diff_daily and
    diff_semesterly. This number is added to the start quota before assertion.
    """
    start = daily_quota(), SEMESTERLY_QUOTA
    assert (
        get_quota(c, user) ==
        UserQuota(user, start[0] + diff_daily, start[1] + diff_semesterly)
    )


@pytest.mark.parametrize('user', ('nobody', 'ckuehl'))
def test_quota_user_not_in_db(user, mysql_connection):
    assert_quota(mysql_connection, user, 0, 0)


def test_pubstaff_has_infinite_quota(mysql_connection):
    assert (
        get_quota(mysql_connection, 'pubstaff') ==
        UserQuota('pubstaff', 500, 500)
    )


def test_semesterly_quota_limits_daily_quota(mysql_connection):
    """The daily quota should be limited by the semesterly quota."""
    assert_quota(mysql_connection, 'nobody', 0, 0)

    DAILY_QUOTA = daily_quota()

    add_job(mysql_connection, TEST_JOB._replace(pages=SEMESTERLY_QUOTA - 5, time=YESTERDAY))
    assert_quota(mysql_connection, 'nobody', -DAILY_QUOTA + 5, -SEMESTERLY_QUOTA + 5)

    add_job(mysql_connection, TEST_JOB._replace(pages=5, time=YESTERDAY))
    assert_quota(mysql_connection, 'nobody', -DAILY_QUOTA, -SEMESTERLY_QUOTA)

    # and now we should hit a floor at zero even if we somehow exceeded the quota
    add_job(mysql_connection, TEST_JOB._replace(pages=3, time=YESTERDAY))
    assert_quota(mysql_connection, 'nobody', -DAILY_QUOTA, -SEMESTERLY_QUOTA)


def test_several_jobs_today(mysql_connection):
    """Multiple jobs should decrease quota correctly."""
    assert_quota(mysql_connection, 'nobody', 0, 0)

    add_job(mysql_connection, TEST_JOB._replace(pages=3))
    assert_quota(mysql_connection, 'nobody', -3, -3)

    add_job(mysql_connection, TEST_JOB._replace(pages=8))
    assert_quota(mysql_connection, 'nobody', -11, -11)

    # now add another user
    assert_quota(mysql_connection, 'somebody', 0, 0)

    add_job(mysql_connection, TEST_JOB._replace(pages=5, user='somebody'))
    assert_quota(mysql_connection, 'somebody', -5, -5)
    assert_quota(mysql_connection, 'nobody', -11, -11)


def test_several_jobs_previous_days_and_semesters(mysql_connection):
    """Multiple jobs should decrease quota correctly over different days,
    semesters, and users."""
    for user in ('nobody', 'somebody', 'yobody'):
        assert_quota(mysql_connection, user, 0, 0)

        # add some jobs today
        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=1, time=TODAY))
        assert_quota(mysql_connection, user, -1, -1)

        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=2, time=TODAY))
        assert_quota(mysql_connection, user, -3, -3)

        # add some jobs yesterday
        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=3, time=YESTERDAY))
        assert_quota(mysql_connection, user, -3, -6)

        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=5, time=YESTERDAY))
        assert_quota(mysql_connection, user, -3, -11)

        # add some jobs last semester
        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=8, time=LAST_SEMESTER))
        assert_quota(mysql_connection, user, -3, -11)

        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=13, time=LAST_SEMESTER))
        assert_quota(mysql_connection, user, -3, -11)


def test_get_quota_user_not_printed_today(mysql_connection):
    """If a user hasn't printed today, we should still be able to get their
    quota."""
    # a user who printed only yesterday
    add_job(mysql_connection, TEST_JOB._replace(user='nobody', pages=13, time=YESTERDAY))
    assert_quota(mysql_connection, 'nobody', 0, -13)

    # a user who printed only last semester
    add_job(mysql_connection, TEST_JOB._replace(user='somebody', pages=13, time=LAST_SEMESTER))
    assert_quota(mysql_connection, 'somebody', 0, 0)


def test_jobs_and_refunds_today(mysql_connection):
    """Refunds should add back pages correctly."""
    assert_quota(mysql_connection, 'nobody', 0, 0)

    add_job(mysql_connection, TEST_JOB._replace(pages=3))
    assert_quota(mysql_connection, 'nobody', -3, -3)

    add_job(mysql_connection, TEST_JOB._replace(pages=5))
    assert_quota(mysql_connection, 'nobody', -8, -8)

    add_refund(mysql_connection, TEST_REFUND._replace(pages=1))
    assert_quota(mysql_connection, 'nobody', -7, -7)

    add_refund(mysql_connection, TEST_REFUND._replace(pages=3))
    assert_quota(mysql_connection, 'nobody', -4, -4)

    # now add another user
    assert_quota(mysql_connection, 'somebody', 0, 0)

    add_job(mysql_connection, TEST_JOB._replace(pages=5, user='somebody'))
    assert_quota(mysql_connection, 'somebody', -5, -5)
    assert_quota(mysql_connection, 'nobody', -4, -4)

    # and some refunds for that user
    add_refund(mysql_connection, TEST_REFUND._replace(pages=8, user='somebody'))
    assert_quota(mysql_connection, 'somebody', 3, 3)
    assert_quota(mysql_connection, 'nobody', -4, -4)

    add_refund(mysql_connection, TEST_REFUND._replace(pages=30, user='somebody'))
    assert_quota(mysql_connection, 'somebody', 33, 33)
    assert_quota(mysql_connection, 'nobody', -4, -4)


def test_several_jobs_refunds_previous_days_and_semesters(mysql_connection):
    """Multiple jobs and refunds should change the quota correctly over
    different days, semesters, and users."""

    for user in ('nobody', 'somebody', 'yobody'):
        assert_quota(mysql_connection, user, 0, 0)

        # add some jobs and refunds today
        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=1, time=TODAY))
        assert_quota(mysql_connection, user, -1, -1)

        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=30))
        assert_quota(mysql_connection, user, 29, 29)

        add_job(mysql_connection, TEST_JOB._replace(user=user, pages=15, time=TODAY))
        assert_quota(mysql_connection, user, 14, 14)

        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=3))
        assert_quota(mysql_connection, user, 17, 17)

        # add some refunds yesterday
        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=3, time=YESTERDAY))
        assert_quota(mysql_connection, user, 17, 20)

        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=8, time=YESTERDAY))
        assert_quota(mysql_connection, user, 17, 28)

        # add some refunds last semester
        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=8, time=LAST_SEMESTER))
        assert_quota(mysql_connection, user, 17, 28)

        add_refund(mysql_connection, TEST_REFUND._replace(user=user, pages=3, time=LAST_SEMESTER))
        assert_quota(mysql_connection, user, 17, 28)


@pytest.fixture(scope='session')
def mysqld_path(tmpdir_factory):
    """Download and extract a local copy of mysqld."""
    tmpdir = tmpdir_factory.mktemp('mysql')
    with tmpdir.as_cwd():
        check_call(('apt-get', 'download', 'mariadb-server-10.0'))
        check_call(('apt-get', 'download', 'mariadb-server-core-10.0'))
        check_call(('apt-get', 'download', 'mariadb-client-core-10.0'))
        for deb in tmpdir.listdir(lambda f: f.fnmatch('*.deb')):
            check_call(('dpkg', '-x', deb.strpath, '.'))
    return tmpdir


@pytest.yield_fixture(scope='session')
def mysqld_socket(mysqld_path, tmpdir_factory):
    """Yield a socket to a running MySQL instance."""
    tmpdir = tmpdir_factory.mktemp('var')
    socket = tmpdir.join('socket')
    data_dir = tmpdir.join('data')
    data_dir.ensure_dir()

    check_call((
        mysqld_path.join('usr', 'bin', 'mysql_install_db').strpath,
        '--no-defaults',
        '--basedir=' + mysqld_path.join('usr').strpath,
        '--datadir=' + data_dir.strpath,
    ))
    proc = Popen((
        mysqld_path.join('usr', 'sbin', 'mysqld').strpath,
        '--no-defaults',
        '--skip-networking',
        '--lc-messages-dir', mysqld_path.join('usr', 'share', 'mysql').strpath,
        '--datadir', data_dir.strpath,
        '--socket', socket.strpath,
    ))

    elapsed = 0
    step = 0.1
    while elapsed < MYSQL_TIMEOUT and not mysql_ready(socket):
        elapsed += step
        time.sleep(step)

    try:
        yield socket
    finally:
        proc.terminate()
        proc.wait()


def mysql_ready(socket):
    try:
        get_connection(socket)
    except pymysql.err.OperationalError:
        return False
    else:
        return True


def get_connection(socket, **kwargs):
    return pymysql.connect(
        user='root',
        password=None,
        unix_socket=socket.strpath,
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
        **kwargs
    )


@pytest.yield_fixture
def mysql_connection(mysqld_path, mysqld_socket, request, tmpdir):
    db_name = 'test_' + ''.join(random.choice(string.ascii_lowercase) for _ in range(20))
    with get_connection(mysqld_socket) as c:
        c.execute('CREATE DATABASE {};'.format(db_name))

    mysql = Popen(
        (
            mysqld_path.join('usr', 'bin', 'mysql').strpath,
            '-h', 'localhost',
            '-u', 'root',
            '--password=',
            '--database', db_name,
            '--socket', mysqld_socket.strpath,
        ),
        stdin=PIPE,
    )
    schema = pkg_resources.resource_string('ocflib.printing', 'ocfprinting.sql')
    schema = schema.replace(  # pretty hacky...
        b'GRANT SELECT ON `ocfprinting`.',
        b'GRANT SELECT ON `' + db_name.encode('ascii') + b'`.',
    )
    mysql.communicate(schema)
    assert mysql.wait() == 0

    with get_connection(mysqld_socket, db=db_name) as c:
        yield c