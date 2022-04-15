import pickle
from typing import List
import numpy as np

from igp2.data.scenario import ScenarioConfig
from igp2.opendrive.map import Map
from sklearn import tree

from grit.core.base import get_data_dir, get_img_dir
from grit.core.data_processing import get_goal_priors, get_dataset, get_multi_scenario_dataset
from grit.decisiontree.decision_tree import Node
from grit.core.feature_extraction import FeatureExtractor
from grit.decisiontree.handcrafted_trees import scenario_trees
from grit.goalrecognition.goal_recognition import FixedGoalRecogniser, GoalRecogniser


class DecisionTreeGoalRecogniser(FixedGoalRecogniser):

    def __init__(self, goal_priors, scenario, decision_trees, goal_locs):
        super().__init__(goal_priors, scenario, goal_locs)
        self.decision_trees = decision_trees

    def goal_likelihood(self, goal_idx, frames, goal, agent_id):
        features = self.feature_extractor.extract(agent_id, frames, goal)
        self.decision_trees[goal_idx][features['goal_type']].reset_reached()
        likelihood = self.decision_trees[goal_idx][features['goal_type']].traverse(features)
        return likelihood

    def goal_likelihood_from_features(self, features, goal_type, goal):
        if goal_type in self.decision_trees[goal]:
            tree = self.decision_trees[goal][goal_type]
            tree_likelihood = tree.traverse(features)
        else:
            tree_likelihood = 0.5
        return tree_likelihood

    @classmethod
    def load(cls, scenario_name):
        priors = cls.load_priors(scenario_name)
        scenario_config = ScenarioConfig.load(f"scenarios/configs/{scenario_name}.json")
        scenario_map = Map.parse_from_opendrive(f"scenarios/maps/{scenario_name}.xodr")
        decision_trees = cls.load_decision_trees(scenario_name)
        return cls(priors, scenario_map, decision_trees, scenario_config.goals)

    @staticmethod
    def load_decision_trees(scenario_name):
        raise NotImplementedError

    @classmethod
    def train(cls, scenario_name, alpha=1, criterion='gini', min_samples_leaf=1,
              max_leaf_nodes=None, max_depth=None, training_set=None, ccp_alpha=0):
        decision_trees = {}
        scenario_config = ScenarioConfig.load(f"scenarios/configs/{scenario_name}.json")

        if training_set is None:
            training_set = get_dataset(scenario_name, subset='train')
        goal_priors = get_goal_priors(training_set, scenario_config.goal_types, alpha=alpha)

        for goal_idx in goal_priors.true_goal.unique():
            decision_trees[goal_idx] = {}
            goal_types = goal_priors.loc[goal_priors.true_goal == goal_idx].true_goal_type.unique()
            for goal_type in goal_types:
                dt_training_set = training_set.loc[(training_set.possible_goal == goal_idx)
                                                   & (training_set.goal_type == goal_type)]
                if dt_training_set.shape[0] > 0:
                    X = dt_training_set[FeatureExtractor.feature_names.keys()].to_numpy()
                    y = (dt_training_set.possible_goal == dt_training_set.true_goal).to_numpy()
                    if y.all() or not y.any():
                        goal_tree = Node(0.5)
                    else:
                        clf = tree.DecisionTreeClassifier(max_leaf_nodes=max_leaf_nodes,
                            min_samples_leaf=min_samples_leaf, max_depth=max_depth, class_weight='balanced',
                            criterion=criterion, ccp_alpha=ccp_alpha)
                        clf = clf.fit(X, y)
                        goal_tree = Node.from_sklearn(clf, FeatureExtractor.feature_names)
                        goal_tree.set_values(dt_training_set, goal_idx, alpha=alpha)
                else:
                    goal_tree = Node(0.5)

                decision_trees[goal_idx][goal_type] = goal_tree
        return cls(goal_priors, scenario_config, decision_trees, scenario_config.goals)

    def save(self, scenario_name):
        for goal_idx in self.goal_priors.true_goal.unique():
            goal_types = self.goal_priors.loc[self.goal_priors.true_goal == goal_idx].true_goal_type.unique()
            for goal_type in goal_types:
                goal_tree = self.decision_trees[goal_idx][goal_type]
                pydot_tree = goal_tree.pydot_tree()
                pydot_tree.write_png(get_img_dir() + 'trained_tree_{}_G{}_{}.png'.format(
                    scenario_name, goal_idx, goal_type))
        with open(get_data_dir() + 'trained_trees_{}.p'.format(scenario_name), 'wb') as f:
            pickle.dump(self.decision_trees, f)
        self.goal_priors.to_csv(get_data_dir() + '{}_priors.csv'.format(scenario_name), index=False)


class HandcraftedGoalTrees(DecisionTreeGoalRecogniser):

    @staticmethod
    def load_decision_trees(scenario_name):
        return scenario_trees[scenario_name]


class Grit(DecisionTreeGoalRecogniser):

    @staticmethod
    def load_decision_trees(scenario_name):
        with open(get_data_dir() + 'trained_trees_{}.p'.format(scenario_name), 'rb') as f:
            return pickle.load(f)


class UniformPriorGrit(Grit):

    def __init__(self, goal_priors, scenario, decision_trees, goal_locs):
        super().__init__(goal_priors, scenario, decision_trees, goal_locs)
        self.goal_priors['prior'] = 1.0 / self.goal_priors.shape[0]


class GeneralisedGrit(GoalRecogniser):

    def __init__(self, priors, decision_trees):
        self.goal_priors = priors
        self.decision_trees = decision_trees

    @classmethod
    def train(cls, scenario_names: List[str], alpha=1, criterion='gini', min_samples_leaf=1,
              max_leaf_nodes=None, max_depth=None, ccp_alpha=0):
        dataset = get_multi_scenario_dataset(scenario_names)
        decision_trees = {}
        goal_types = dataset.goal_type.unique()
        for goal_type in goal_types:
            dt_training_set = dataset.loc[dataset.goal_type == goal_type]
            if dt_training_set.shape[0] > 0:
                X = dt_training_set[FeatureExtractor.feature_names.keys()].to_numpy()
                y = (dt_training_set.possible_goal == dt_training_set.true_goal).to_numpy()
                if y.all() or not y.any():
                    goal_tree = Node(0.5)
                else:
                    clf = tree.DecisionTreeClassifier(max_leaf_nodes=max_leaf_nodes,
                                                      min_samples_leaf=min_samples_leaf, max_depth=max_depth,
                                                      class_weight='balanced',
                                                      criterion=criterion, ccp_alpha=ccp_alpha)
                    clf = clf.fit(X, y)
                    goal_tree = Node.from_sklearn(clf, FeatureExtractor.feature_names)
                    goal_tree.set_values(dt_training_set, goal_type, alpha=alpha)
            else:
                goal_tree = Node(0.5)

            decision_trees[goal_type] = goal_tree
        priors = np.ones(len(decision_trees)) / len(decision_trees)
        return cls(priors, decision_trees)

    def save(self):
        for goal_type, goal_tree in self.decision_trees.items():
            pydot_tree = goal_tree.pydot_tree()
            pydot_tree.write_png(get_img_dir() + f'trained_tree_{goal_type}.png')
        with open(get_data_dir() + 'trained_trees_generalised.p', 'wb') as f:
            pickle.dump(self.decision_trees, f)

    def goal_likelihood_from_features(self, features, goal_type, goal):
        if goal_type in self.decision_trees:
            tree = self.decision_trees[goal_type]
            tree_likelihood = tree.traverse(features)
        else:
            tree_likelihood = 0.5
        return tree_likelihood

    @classmethod
    def load(cls, scenario_name):
        with open(get_data_dir() + 'trained_trees_generalised.p', 'rb') as f:
            decision_trees = pickle.load(f)
        priors = np.ones(len(decision_trees)) / len(decision_trees)
        return cls(priors, decision_trees)