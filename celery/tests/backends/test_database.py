from __future__ import absolute_import, unicode_literals

from datetime import datetime

from pickle import loads, dumps

from celery import states
from celery.exceptions import ImproperlyConfigured
from celery.utils import uuid

from celery.tests.case import (
    AppCase,
    Mock,
    SkipTest,
    depends_on_current_app,
    patch,
    skip_if_pypy,
    skip_if_jython,
)

try:
    import sqlalchemy  # noqa
except ImportError:
    DatabaseBackend = Task = TaskSet = retry = None  # noqa
    SessionManager = session_cleanup = None  # noqa
else:
    from celery.backends.database import (
        DatabaseBackend, retry, session_cleanup,
    )
    from celery.backends.database import session
    from celery.backends.database.session import SessionManager
    from celery.backends.database.models import Task, TaskSet


class SomeClass(object):

    def __init__(self, data):
        self.data = data


class test_session_cleanup(AppCase):

    def setup(self):
        if session_cleanup is None:
            raise SkipTest('slqlalchemy not installed')

    def test_context(self):
        session = Mock(name='session')
        with session_cleanup(session):
            pass
        session.close.assert_called_with()

    def test_context_raises(self):
        session = Mock(name='session')
        with self.assertRaises(KeyError):
            with session_cleanup(session):
                raise KeyError()
        session.rollback.assert_called_with()
        session.close.assert_called_with()


class test_DatabaseBackend(AppCase):

    @skip_if_pypy
    @skip_if_jython
    def setup(self):
        if DatabaseBackend is None:
            raise SkipTest('sqlalchemy not installed')
        self.uri = 'sqlite:///test.db'
        self.app.conf.result_serializer = 'pickle'

    def test_retry_helper(self):
        from celery.backends.database import DatabaseError

        calls = [0]

        @retry
        def raises():
            calls[0] += 1
            raise DatabaseError(1, 2, 3)

        with self.assertRaises(DatabaseError):
            raises(max_retries=5)
        self.assertEqual(calls[0], 5)

    def test_missing_dburi_raises_ImproperlyConfigured(self):
        self.app.conf.sqlalchemy_dburi = None
        with self.assertRaises(ImproperlyConfigured):
            DatabaseBackend(app=self.app)

    def test_missing_task_id_is_PENDING(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        self.assertEqual(tb.get_status('xxx-does-not-exist'), states.PENDING)

    def test_missing_task_meta_is_dict_with_pending(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        self.assertDictContainsSubset({
            'status': states.PENDING,
            'task_id': 'xxx-does-not-exist-at-all',
            'result': None,
            'traceback': None
        }, tb.get_task_meta('xxx-does-not-exist-at-all'))

    def test_mark_as_done(self):
        tb = DatabaseBackend(self.uri, app=self.app)

        tid = uuid()

        self.assertEqual(tb.get_status(tid), states.PENDING)
        self.assertIsNone(tb.get_result(tid))

        tb.mark_as_done(tid, 42)
        self.assertEqual(tb.get_status(tid), states.SUCCESS)
        self.assertEqual(tb.get_result(tid), 42)

    def test_is_pickled(self):
        tb = DatabaseBackend(self.uri, app=self.app)

        tid2 = uuid()
        result = {'foo': 'baz', 'bar': SomeClass(12345)}
        tb.mark_as_done(tid2, result)
        # is serialized properly.
        rindb = tb.get_result(tid2)
        self.assertEqual(rindb.get('foo'), 'baz')
        self.assertEqual(rindb.get('bar').data, 12345)

    def test_mark_as_started(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        tid = uuid()
        tb.mark_as_started(tid)
        self.assertEqual(tb.get_status(tid), states.STARTED)

    def test_mark_as_revoked(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        tid = uuid()
        tb.mark_as_revoked(tid)
        self.assertEqual(tb.get_status(tid), states.REVOKED)

    def test_mark_as_retry(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        tid = uuid()
        try:
            raise KeyError('foo')
        except KeyError as exception:
            import traceback
            trace = '\n'.join(traceback.format_stack())
            tb.mark_as_retry(tid, exception, traceback=trace)
            self.assertEqual(tb.get_status(tid), states.RETRY)
            self.assertIsInstance(tb.get_result(tid), KeyError)
            self.assertEqual(tb.get_traceback(tid), trace)

    def test_mark_as_failure(self):
        tb = DatabaseBackend(self.uri, app=self.app)

        tid3 = uuid()
        try:
            raise KeyError('foo')
        except KeyError as exception:
            import traceback
            trace = '\n'.join(traceback.format_stack())
            tb.mark_as_failure(tid3, exception, traceback=trace)
            self.assertEqual(tb.get_status(tid3), states.FAILURE)
            self.assertIsInstance(tb.get_result(tid3), KeyError)
            self.assertEqual(tb.get_traceback(tid3), trace)

    def test_forget(self):
        tb = DatabaseBackend(self.uri, backend='memory://', app=self.app)
        tid = uuid()
        tb.mark_as_done(tid, {'foo': 'bar'})
        tb.mark_as_done(tid, {'foo': 'bar'})
        x = self.app.AsyncResult(tid, backend=tb)
        x.forget()
        self.assertIsNone(x.result)

    def test_process_cleanup(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        tb.process_cleanup()

    @depends_on_current_app
    def test_reduce(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        self.assertTrue(loads(dumps(tb)))

    def test_save__restore__delete_group(self):
        tb = DatabaseBackend(self.uri, app=self.app)

        tid = uuid()
        res = {'something': 'special'}
        self.assertEqual(tb.save_group(tid, res), res)

        res2 = tb.restore_group(tid)
        self.assertEqual(res2, res)

        tb.delete_group(tid)
        self.assertIsNone(tb.restore_group(tid))

        self.assertIsNone(tb.restore_group('xxx-nonexisting-id'))

    def test_cleanup(self):
        tb = DatabaseBackend(self.uri, app=self.app)
        for i in range(10):
            tb.mark_as_done(uuid(), 42)
            tb.save_group(uuid(), {'foo': 'bar'})
        s = tb.ResultSession()
        for t in s.query(Task).all():
            t.date_done = datetime.now() - tb.expires * 2
        for t in s.query(TaskSet).all():
            t.date_done = datetime.now() - tb.expires * 2
        s.commit()
        s.close()

        tb.cleanup()

    def test_Task__repr__(self):
        self.assertIn('foo', repr(Task('foo')))

    def test_TaskSet__repr__(self):
        self.assertIn('foo', repr(TaskSet('foo', None)))


class test_SessionManager(AppCase):

    def setup(self):
        if SessionManager is None:
            raise SkipTest('sqlalchemy not installed')

    def test_after_fork(self):
        s = SessionManager()
        self.assertFalse(s.forked)
        s._after_fork()
        self.assertTrue(s.forked)

    @patch('celery.backends.database.session.create_engine')
    def test_get_engine_forked(self, create_engine):
        s = SessionManager()
        s._after_fork()
        engine = s.get_engine('dburi', foo=1)
        create_engine.assert_called_with('dburi', foo=1)
        self.assertIs(engine, create_engine())
        engine2 = s.get_engine('dburi', foo=1)
        self.assertIs(engine2, engine)

    @patch('celery.backends.database.session.sessionmaker')
    def test_create_session_forked(self, sessionmaker):
        s = SessionManager()
        s.get_engine = Mock(name='get_engine')
        s._after_fork()
        engine, session = s.create_session('dburi', short_lived_sessions=True)
        sessionmaker.assert_called_with(bind=s.get_engine())
        self.assertIs(session, sessionmaker())
        sessionmaker.return_value = Mock(name='new')
        engine, session2 = s.create_session('dburi', short_lived_sessions=True)
        sessionmaker.assert_called_with(bind=s.get_engine())
        self.assertIsNot(session2, session)
        sessionmaker.return_value = Mock(name='new2')
        engine, session3 = s.create_session(
            'dburi', short_lived_sessions=False)
        sessionmaker.assert_called_with(bind=s.get_engine())
        self.assertIs(session3, session2)

    def test_coverage_madness(self):
        prev, session.register_after_fork = (
            session.register_after_fork, None,
        )
        try:
            SessionManager()
        finally:
            session.register_after_fork = prev
