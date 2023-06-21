from __future__ import annotations
from cosmap.analysis.sampler import Sampler
from cosmap.dataset import get_dataset
from cosmap.analysis import task
from cosmap.locations import ROOT
from cosmap.output import get_output_handler
from cosmap.analysis import dependencies
from dask.distributed import Client
from pydantic import BaseModel
from cosmap.analysis.setup import handle_setup
from cosmap.analysis import plugins
from loguru import logger
import threading
class AnalysisException(Exception):
    pass


    
class CosmapAnalysis:
    """
    The Analysis class is the central class of Cosmap. It defines
    a series of transformations which are applied in a particular sequence
    (or in parallel, where appropriate) to transform data from one form to
    another. 

    Cosmap is generally designed for analsyes that involve pulling data in some
    region from a dataset, performing some computation on that data, and then
    repeating many times.

    Distributed computing is handled by dask. Documentation to come...


    """
    ignore_blocks = ["Setup", "Teardown"]
    def __init__(self, analysis_paramters: BaseModel, plugins = {}, **kwargs):
        self.parameters = analysis_paramters
        self.plugins = plugins

        self.setup()
 

    def setup(self, *args, **kwargs):
        self.verify_analysis()
        if self.plugins:
            plugins.verify_plugins(self.plugins, self.parameters.analysis_parameters.definition_module)
            self.plugins = plugins.initialize_plugins(self, self.plugins, self.parameters)

        self.dataset_plugin = get_dataset(self.parameters.dataset_parameters)
        self.sampler = Sampler(self.parameters.sampling_parameters, self.plugins.get("sampler", {}))


        self.sampler.initialize_sampler()
        samples = self.sampler.generate_samples(self.parameters.sampling_parameters.n_samples)
        blocks = []
        if "Setup" in self.parameters.analysis_parameters.transformations:
            new_params = handle_setup(self.parameters, self.parameters.analysis_parameters.transformations)
            new_param_input = {}
            new_analysis_parameters = {}
            for name, block in new_params.items():
                if name.split(".")[0] == "Main":
                    new_param_input.update({".".join(name.split(".")[1:]): block})
                else:
                    new_analysis_parameters.update({name: block})
            if new_analysis_parameters:
                new_param_input.update({"analysis_parameters": new_analysis_parameters})
            
            self.parameters = self.update_parameters(self.parameters, new_param_input)

        transformations = self.parameters.analysis_parameters.transformations["Main"]

        self.needed_datatypes = [t.get("needed-data", []) for t in transformations.values()]
        self.needed_datatypes = set([item for sublist in self.needed_datatypes for item in sublist])
        self.parameters.sampling_parameters.dtypes = self.needed_datatypes
        self.output_handler = get_output_handler(self.parameters.output_parameters)
        self.client = Client(n_workers = self.parameters.threads - 1, threads_per_worker = 1)
        self.client.register_worker_plugin(self.dataset_plugin)
        self.tasks = task.generate_tasks(self.client, self.parameters, self.main_graph, self.needed_datatypes, samples, plugins=self.plugins)

    def verify_analysis(self):
        """
        Verify that the analysis is valid. By the time we get here, we already know that all of the configuraiton
        is valid, since it had to be parsed by Pydantic. This function checks that the analysis itself is valid, meaning
        that it has a valid DAG structure, all transformations defined in the config are implementation file, and that all
        transformations take parameters that actually exist (or, will be created by a previous transformation)
        """
        transformations = self.parameters.analysis_parameters.transformations.get("Main", {})
        if not transformations:
            raise AnalysisException("No transformations defined in transformations.json!")
        self.main_graph = dependencies.build_dependency_graphs(self.parameters.analysis_parameters.transformations, block_="Main")["Main"]
        #Note, the build_dependency_graphs function will raise an exception if the graph is not a DAG
        #So we don't need to check that here
        definitions = self.parameters.analysis_parameters.definition_module.transformations
        try:
            main_definitions = definitions.Main
        except AttributeError:
            raise AnalysisException("No Main block found in transformations.py!")
        for name, block in transformations.items():
            try:
                getattr(main_definitions, name)
            except AttributeError:
                raise AnalysisException(f"Could not find the definition for transformation {name} in the \'Main\' block of transformations.py!")

    @staticmethod
    def update_parameters(old_paramters, new_params: dict):
        for name, values in new_params.items():
            p_obj = old_paramters
            param_path = name.split(".")
            for p in param_path[:-1]:
                p_obj = getattr(p_obj, p)
            if not hasattr(p_obj, param_path[-1]):
                #We're attaching extra parameters. The block must
                #explicitly allow this, or Pydantic will throw an error
                setattr(p_obj, param_path[-1], values)
                continue

            if isinstance(getattr(p_obj, param_path[-1]), BaseModel):
                block = getattr(p_obj, name)
                updated_block = CosmapAnalysis.update_parameters(block, values)
                setattr(p_obj, name, updated_block)
            else:
                setattr(p_obj, param_path[-1], values)
        return old_paramters
    

    def run(self, *args, **kwargs):
        n_completed = 0
        for chunk in self.tasks:
            results = self.client.gather(chunk)
            #Note: this is an array of arrays, so we flatten it
            results = [item for sublist in results for item in sublist]
            n_completed += len(results)
            self.output_handler.take_outputs(results)
            logger.info(f"Completed {n_completed} of {self.parameters.sampling_parameters.n_samples} samples")
            self.output_handler.write_output()