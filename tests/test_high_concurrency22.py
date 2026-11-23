import threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from jyexport.execution import ExecutionBudget,LimitGate,concurrency,performance_limits
from jyexport.resources import RenderAdmission,ResourcePressure,machine_render_lock
from jyexport.storage import Cancelled


class HighConcurrencyTests(unittest.TestCase):
    def test_eight_slots_are_real_and_never_exceeded(self):
        budget=ExecutionBudget(8);barrier=threading.Barrier(8);lock=threading.Lock();active=0;peak=0
        def render(number):
            nonlocal active,peak
            with budget.native.slot():
                with lock:active+=1;peak=max(peak,active)
                if number<8:barrier.wait(3)
                time.sleep(.01)
                with lock:active-=1
        with ThreadPoolExecutor(max_workers=12) as pool:list(pool.map(render,range(24)))
        self.assertEqual(peak,8);self.assertEqual(budget.native.snapshot()['active'],0)
        self.assertEqual(budget.native.snapshot()['peak_render'],8)
        self.assertEqual(budget.native.snapshot()['calls'],24)
    def test_limit_validation_and_legacy_default_migration(self):
        for bad in (0,9,-1,True,4.,'4'):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):concurrency(bad,'test')
        self.assertEqual(performance_limits({}),(4,4))
        self.assertEqual(performance_limits({'batch_jobs':2,'native_limit':2}),(4,4))
        self.assertEqual(performance_limits({'batch_jobs':1,'native_limit':1}),(1,1))
        self.assertEqual(performance_limits({'batch_jobs':2,'native_limit':2,'performance_settings_version':2}),(2,2))
        self.assertEqual(performance_limits({'batch_jobs':8,'native_limit':6}),(8,6))
    def test_duplicate_resource_errors_do_not_double_reduce_one_wave(self):
        budget=ExecutionBudget(8)
        self.assertTrue(budget.reduce(8,'first failure'))
        self.assertFalse(budget.reduce(8,'same-wave failure'))
        self.assertEqual(budget.native.snapshot()['limit'],4)
        self.assertTrue(budget.reduce(4));self.assertTrue(budget.reduce(2));self.assertFalse(budget.reduce(1))
        self.assertEqual([(r['before'],r['after']) for r in budget.reductions],[(8,4),(4,2),(2,1)])
    def test_low_memory_wait_can_cancel_without_launch_or_leaked_reservation(self):
        cancel=threading.Event();probe=lambda:{'physical_available_mib':100,'commit_available_mib':100,'gpus':[]}
        admission=RenderAdmission(probe,timeout=5);gate=LimitGate(8,cancel,admission);outcome=[]
        def render():
            try:
                with gate.slot():outcome.append('incorrect launch')
            except Cancelled:outcome.append('cancelled')
        thread=threading.Thread(target=render);thread.start()
        deadline=time.monotonic()+1
        while gate.snapshot()['queued']!=1 and time.monotonic()<deadline:time.sleep(.005)
        cancel.set();thread.join(2)
        self.assertEqual(outcome,['cancelled']);self.assertEqual(gate.snapshot()['active'],0)
        self.assertEqual(gate.snapshot()['queued'],0);self.assertEqual(admission.starts,{})
    def test_gpu_pressure_and_unknown_memory_do_not_admit_unbounded_work(self):
        data={'physical_available_mib':16000,'commit_available_mib':24000,'gpus':[{'name':'fixture','free_mib':900}]}
        admission=RenderAdmission(lambda:data)
        self.assertIn('显存',admission.admit('render',2))
        self.assertEqual(admission.admit('helper',0),'')
        unknown=RenderAdmission(lambda:{'gpus':[]})
        self.assertEqual(unknown.admit('render',1),'')
        self.assertIn('最多2',unknown.admit('render',2))
    def test_startup_reserves_cover_counter_lag_and_are_released(self):
        data={'physical_available_mib':4096,'commit_available_mib':16000,'gpus':[]}
        admission=RenderAdmission(lambda:data)
        for index in range(3):admission.started(index,'render')
        self.assertIn('内存',admission.admit('render',3))
        for index in range(3):admission.finished(index)
        self.assertEqual(admission.admit('render',0),'')
    def test_sustained_critical_memory_reports_actionable_error(self):
        admission=RenderAdmission(lambda:{'physical_available_mib':1,'gpus':[]},timeout=0)
        with self.assertRaisesRegex(ResourcePressure,'继续本批次'):admission.admit('render',0)
    def test_renderers_and_validators_share_atomic_memory_reservations(self):
        data={'physical_available_mib':3000,'commit_available_mib':12000,'gpus':[{'name':'fixture','free_mib':10}]}
        admission=RenderAdmission(lambda:data)
        admission.started('a','render');admission.started('b','render')
        self.assertIn('内存',admission.admit('validation',0))
        admission.finished('a')
        self.assertEqual(admission.admit('validation',0,token='v'),'')
        self.assertIn('v',admission.starts)
        admission.finished('v');admission.finished('b');self.assertEqual(admission.starts,{})
    def test_another_window_cannot_multiply_the_machine_budget(self):
        outcome=[]
        def other_window():
            try:
                with machine_render_lock():outcome.append('incorrectly acquired')
            except (ValueError,RuntimeError):outcome.append('blocked')
        with machine_render_lock():
            thread=threading.Thread(target=other_window);thread.start();thread.join(2)
        self.assertEqual(outcome,['blocked'])
        with machine_render_lock():pass


if __name__=='__main__':unittest.main()
