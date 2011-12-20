#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

import util
import multiprocessing
import multiprocessing.managers
import cPickle as pickle
import Queue

class Dispatcher(object):
    """This class coordinates the work of all the TileSet objects
    among one worker process. By subclassing this class and
    implementing setup_tilesets(), dispatch(), finish_work() and
    close(), it is possible to create a Dispatcher that distributes
    this work to many worker processes.
    """
    def render_all(self, tilesetlist, status_callback):
        """Render all of the tilesets in the given
        tilesetlist. status_callback is called periodically to update
        status.  """
        # TODO use status callback
        
        # setup tilesetlist
        self.setup_tilesets(tilesetlist)
        
        # preprocessing
        for tileset in tilesetlist:
            tileset.do_preprocessing()
        
        # iterate through all possible phases
        num_phases = [tileset.get_num_phases() for tileset in tilesetlist]
        for phase in xrange(max(num_phases)):
            # construct a list of iterators to use for this phase
            work_iterators = []
            for i, tileset in enumerate(tilesetlist):
                if phase < num_phases[i]:
                    def make_work_iterator(tset, p):
                        return ((tset, workitem) for workitem in tset.iterate_work_items(p))
                    work_iterators.append(make_work_iterator(tileset, phase))
            
            # go through these iterators round-robin style
            for tileset, workitem in util.roundrobin(work_iterators):
                self.dispatch(tileset, workitem)
            
            # after each phase, wait for the work to finish
            self.finish_work()
    
    def close(self):
        """Close the Dispatcher. This should be called when you are
        done with the dispatcher, to ensure that it cleans up any
        processes or connections it may still have around.
        """
        pass
    
    def setup_tilesets(self, tilesetlist):
        """Called whenever a new list of tilesets are being used. This
        lets subclasses distribute the whole list at once, instead of
        for each work item."""
        pass
    
    def dispatch(self, tileset, workitem):
        """Dispatch the given work item. The end result of this call
        should be running tileset.do_work(workitem) somewhere.
        """
        tileset.do_work(workitem)
    
    def finish_work(self):
        """This call should block until all dispatched jobs have
        completed. It's used at the end of each phase to ensure that
        phases are always run in serial.
        """
        pass

class MultiprocessingDispatcherManager(multiprocessing.managers.SyncManager):
    """This multiprocessing manager is responsible for giving worker
    processes access to the communication Queues, and also gives
    workers access to the current tileset list.
    """
    def __init__(self):
        self.job_queue = multiprocessing.Queue()
        self.result_queue = multiprocessing.Queue()
        
        self.register("get_job_queue", callable=lambda: self.job_queue)
        self.register("get_result_queue", callable=lambda: self.result_queue)
        
        # SyncManager must be initialized to create the list below
        super(MultiprocessingDispatcherManager, self).__init__()
        self.start()
        
        self.tilesets = []
        self.tileset_version = 0
        self.tileset_data = self.list([[], 0])        
    
    def set_tilesets(self, tilesets):
        """This is used in MultiprocessingDispatcher.setup_tilesets to
        update the tilesets each worker has access to. It also
        increments a `tileset_version` which is an easy way for
        workers to see if their tileset list is out-of-date without
        pickling and copying over the entire list.
        """
        self.tilesets = tilesets
        self.tileset_version += 1
        self.tileset_data[0] = self.tilesets
        self.tileset_data[1] = self.tileset_version
    
    def get_tilesets(self):
        """This returns a (tilesetlist, tileset_version) tuple when
        called from a worker process.
        """
        return self.tileset_data._getvalue()

class MultiprocessingDispatcherProcess(multiprocessing.Process):
    """This class represents a single worker process. It is created
    automatically by MultiprocessingDispatcher, but it can even be
    used manually to spawn processes on different machines on the same
    network.
    """
    def __init__(self, manager):
        """Creates the process object. manager should be an instance
        of MultiprocessingDispatcherManager connected to the one
        created in MultiprocessingDispatcher.
        """
        super(MultiprocessingDispatcherProcess, self).__init__()
        self.manager = manager
        self.job_queue = manager.get_job_queue()
        self.result_queue = manager.get_result_queue()
    
    def update_tilesets(self):
        """A convenience function to update our local tilesets to the
        current version in use by the MultiprocessingDispatcher.
        """
        self.tilesets, self.tileset_version = self.manager.get_tilesets()
    
    def run(self):
        """The main work loop. Jobs are pulled from the job queue and
        executed, then the result is pushed onto the result
        queue. Updates to the tilesetlist are recognized and handled
        automatically. This is the method that actually runs in the
        new worker process.
        """
        timeout = 1.0
        self.update_tilesets()
        while True:
            try:
                job = self.job_queue.get(True, timeout)
                if job == None:
                    # this is a end-of-jobs sentinel
                    return
                
                # unpack job
                tv, ti, workitem = job
                
                if tv != self.tileset_version:
                    # our tilesets changed!
                    self.update_tilesets()
                    assert tv == self.tileset_version
                
                # do job
                result = self.tilesets[ti].do_work(workitem)
                self.result_queue.put(result, False)
            except Queue.Empty:
                pass

class MultiprocessingDispatcher(Dispatcher):
    """A subclass of Dispatcher that spawns worker processes and
    distributes jobs to them to speed up processing.
    """
    def __init__(self, local_procs=0):
        """Creates the dispatcher. local_procs should be the number of
        worker processes to spawn. If it's omitted (or non-positive)
        the number of available CPUs is used instead.
        """
        
        # automatic local_procs handling
        if local_procs <= 0:
            local_procs = multiprocessing.cpu_count()
        self.local_procs = local_procs
        
        self.outstanding_jobs = 0        
        self.manager = MultiprocessingDispatcherManager()
        self.job_queue = self.manager.job_queue
        self.result_queue = self.manager.result_queue
        
        # create and fill the pool
        self.pool = []
        for i in xrange(self.local_procs):
            proc = MultiprocessingDispatcherProcess(self.manager)
            proc.start()
            self.pool.append(proc)
    
    def close(self):
        # send of the end-of-jobs sentinel
        for p in self.pool:
            self.job_queue.put(None)
        
        # and close the manager
        self.manager.shutdown()
        self.manager = None
        self.pool = None
    
    def setup_tilesets(self, tilesets):
        self.manager.set_tilesets(tilesets)
    
    def dispatch(self, tileset, workitem):
        # create and submit the job
        tileset_index = self.manager.tilesets.index(tileset)
        self.job_queue.put((self.manager.tileset_version, tileset_index, workitem))
        self.outstanding_jobs += 1
        
        # make sure the queue doesn't fill up too much
        while self.outstanding_jobs > self.local_procs * 10:
            self._handle_messages()
    
    def finish_work(self):
        # empty the queue
        while self.outstanding_jobs > 0:
            self._handle_messages()
    
    def _handle_messages(self):
        # work function: takes results out of the result queue and
        # keeps track of how many outstanding jobs remain
        timeout = 1.0
        try:
            while True: # exits in except block
                result = self.result_queue.get(True, timeout)
                # timeout should only apply once
                timeout = 0.0
                
                self.outstanding_jobs -= 1
        except Queue.Empty:
            pass