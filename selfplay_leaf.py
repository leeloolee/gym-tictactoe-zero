# -*- coding: utf-8 -*-
import tictactoe_env
import neural_network

import time
import xxhash
from collections import deque, defaultdict

import torch
from torch.autograd import Variable
# from torch.optim import lr_scheduler

import slackweb
import dill as pickle
import numpy as np
np.set_printoptions(suppress=True)

PLAYER = 0
OPPONENT = 1
MARK_O = 0
MARK_X = 1
N, W, Q, P = 0, 1, 2, 3

PLAY = 1
SIMULATION = 800
SAVE_CYCLE = 800

NUM_CHANNEL = 128

PLANE = np.zeros((3, 3), 'int').flatten()


class MCTS(object):
    """몬테카를로 트리 탐색 클래스

    시뮬레이션을 통해 train 데이터 생성 (state, edge 저장)

    state
    ------
    각 주체당 4수까지 저장해서 state_new 로 만듦

        9x3x3 numpy array -> 1x81 tuple

    edge
    -----
    현재 state에서 착수 가능한 모든 action자리에 4개의 정보 저장

    type: 3x3x4 numpy array

        9개 좌표에 4개의 정보 N, W, Q, P 매칭
        N: edge 방문횟수, W: 보상누적값, Q: 보상평균값(W/N), P: 선택 확률 추정치
        edge[좌표행][좌표열][번호]로 접근

    """

    def __init__(self):
        # ROM
        self.state_memory = deque(maxlen=9 * PLAY)
        self.tree_memory = defaultdict(lambda: np.zeros((3, 3, 4), 'float'))

        # model
        self.pv_net = neural_network.PolicyValueNet(NUM_CHANNEL)

        # hyperparameter
        self.c_puct = 5
        self.epsilon = 0.25
        self.alpha = 0.7

        # reset_step member
        self.edge = None
        self.node = None
        self.puct = None
        self.total_visit = None
        self.empty_loc = None
        self.state = None
        self.state_tensor = None
        self.state_variable = None
        self.p_theta = None
        self.value = None
        self.pr = None
        self.edge_n = None
        self.pi = None

        # reset_episode member
        self.player_history = None
        self.opponent_history = None
        self.node_memory = None
        self.edge_memory = None
        self.action_memory = None
        self.value_memory = None
        self.pi_memory = None
        self.action_count = None
        self.board_fill = None
        self.mark_o = None
        self.user_type = None
        self.done = None
        self.state_new = None

        # member init
        self._reset_step()
        self._reset_episode()

    def _reset_step(self):
        self.edge = np.zeros((3, 3, 4), 'float')
        self.node = None
        self.puct = np.zeros((3, 3), 'float')
        self.total_visit = 0
        self.empty_loc = None
        self.state = None
        self.state_tensor = None
        self.state_variable = None
        self.p_theta = None
        self.value = None
        self.pr = np.zeros((3, 3), 'float')
        self.edge_n = np.zeros((3, 3), 'float')
        self.pi = None

    def _reset_episode(self):
        self.player_history = deque([PLANE] * 4, maxlen=4)
        self.opponent_history = deque([PLANE] * 4, maxlen=4)
        self.node_memory = deque(maxlen=9)
        self.edge_memory = deque(maxlen=9)
        self.action_memory = deque(maxlen=9)
        self.value_memory = deque(maxlen=9)
        self.action_count = 0
        self.state_new = None
        self.board_fill = None
        self.mark_o = None
        self.user_type = None
        self.done = False

    def select_action(self, state):
        """raw state를 받아 변환 및 저장 후 action을 리턴하는 외부 메소드.

        state 변환
        ---------
        state_new -> node & state_variable

            state_new: 9x3x3 numpy array.
                유저별 최근 4-histroy 저장하여 재구성. (저장용)

            state_variable: 1x9x3x3 torch.autograd.Variable.
                신경망의 인수로 넣을 수 있게 조정. (학습용)

            node: string. (xxhash)
                state_new를 string으로 바꾼 후 hash 생성. (탐색용)

        action 선택
        -----------
        puct 값이 가장 높은 곳을 선택함, 동점이면 랜덤 선택.

            action: 1x3 tuple.
            action = (피아식별, 보드의 x좌표, 보드의 y좌표)

        """
        # action 수 세기
        self.action_count += 1

        # 호출될 때마다 첫턴 기준 교대로 행동주체 바꿈, 최종 action에 붙여줌
        # PLAYER or OPPONENT
        self.user_type = (self.mark_o + self.action_count - 1) % 2

        # state 변환
        self.state = state
        self.state_new = self._convert_state(state)

        # 현재 보드에서 착수가능한 수 저장
        self.board_fill = self.state[PLAYER] + self.state[OPPONENT]
        self.empty_loc = np.argwhere(self.board_fill == 0)

        # root node면 저장
        if self.action_count == 1:
            self.state_memory.appendleft(self.state_new)

        # state -> 문자열 -> hash로 변환 후 저장 (new state 대신 dict의 key로 사용)
        self.node = xxhash.xxh64(self.state_new.tostring()).hexdigest()
        self.node_memory.appendleft(self.node)

        # tree 탐색 -> edge 생성 or 호출 -> 각 edge의 PUCT 계산
        self._tree_search()

        # 빈자리가 아닌 곳은 PUCT값으로 -inf를 넣어 빈자리가 최댓값이 되는 것 방지
        puct = self.puct.tolist()
        for i, v in enumerate(puct):
            for j, _ in enumerate(v):
                if [i, j] not in self.empty_loc.tolist():
                    self.puct[i][j] = -np.inf

        # N값이 0인 edge의 PUCT값으로 -inf를 넣어 무작위 확장 방지
        edge_n = self.edge_n.tolist()
        for i, v in enumerate(edge_n):
            for j, _ in enumerate(v):
                if self.edge_n[i][j] == 0:
                    self.puct[i][j] = -np.inf

        # PUCT 점수 출력
        print('***  PUCT Score  ***')
        print(self.puct.round(decimals=2))

        # PUCT가 최댓값인 곳 찾기
        self.puct = np.asarray(puct)
        puct_max = np.argwhere(self.puct == self.puct.max()).tolist()

        # 최댓값 동점인 곳 처리
        move_target = puct_max[np.random.choice(len(puct_max))]

        # 최종 action 구성 (행동주체 + 좌표) 접붙히기
        action = np.r_[self.user_type, move_target]

        # action 저장 및 step member 초기화
        self.action_memory.appendleft(action)

        # tuple로 action 리턴
        return tuple(action)

    def _convert_state(self, state):
        """state변환 메소드: action 주체별 최대 4수까지 history를 저장하여 새로운 state로 변환."""
        if abs(self.user_type) == OPPONENT:
            self.player_history.appendleft(state[PLAYER].flatten())
        else:
            self.opponent_history.appendleft(state[OPPONENT].flatten())
        state_new = np.r_[np.array(self.player_history).flatten(),
                          np.array(self.opponent_history).flatten(),
                          self.state[2].flatten()]
        return state_new

    def _tree_search(self):
        """tree search를 통해 선택, 확장하는 메소드.

        {node: edge}인 Tree 구성
        edge에 있는 Q, P를 이용하여 PUCT값을 계산한 뒤 모든 좌표에 매칭.

        """
        # tree에서 현재 node를 검색하여 존재하면 해당 edge [선택]
        if self.node in self.tree_memory:
            self.edge = self.tree_memory[self.node]
            print('"Select"')
            for i in range(3):
                for j in range(3):
                    self.pr[i][j] = self.edge[i][j][P]
                    self.edge_n[i][j] = self.edge[i][j][N]

            if np.sum(self.edge_n) >= 1:
                # root node면 edge의 P에 노이즈 (탐험)
                if self.action_count == 1:
                    print('"root node"')
                    pr = (1 - self.epsilon) * self.pr.flatten() + \
                        self.epsilon * np.random.dirichlet(
                        self.alpha * np.ones(9))
                    self.pr = pr.reshape(3, 3)
                    # P값 재배치
                    for i in range(3):
                        for j in range(3):
                            self.edge[i][j][P] = self.pr[i][j]
            else:
                print('"leaf node"')
                self._expand(self.node)
        else:
            print('"child node"')
            self.done = True

        # Q값 계산 후 배치
        for i in range(3):
            for j in range(3):
                if self.edge[i][j][N] != 0:
                    self.edge[i][j][Q] = self.edge[i][j][W] / \
                        self.edge[i][j][N]

                # 각자리의 PUCT 계산
                self.puct[i][j] = self.edge[i][j][Q] + \
                    self.c_puct * \
                    self.edge[i][j][P] * \
                    np.sqrt(np.sum(self.edge_n)) / (1 + self.edge[i][j][N])

        # Q, P값을 배치한 edge 시뮬레이션 동안 저장
        self.edge_memory.appendleft(self.edge)

    def _expand(self, edge):
        # edge를 생성
        self.edge = self.tree_memory[self.node]
        # state에 Variable 씌워서 신경망에 넣기
        self.state_tensor = torch.from_numpy(self.state_new)
        self.state_variable = Variable(
            self.state_tensor.view(
                9, 3, 3).float().unsqueeze(0))
        # 아웃풋 받기 (p, v)
        self.p_theta, self.value = self.pv_net(self.state_variable)
        self.pr = self.p_theta.data.numpy().reshape(3, 3)
        # root node면 edge의 P에 노이즈 (탐험)
        if self.action_count == 1:
            pr = (1 - self.epsilon) * self.pr.flatten() + \
                self.epsilon * np.random.dirichlet(
                self.alpha * np.ones(9))
            self.pr = pr.reshape(3, 3)
        # P값 배치
        for i in range(3):
            for j in range(3):
                self.edge[i][j][P] = self.pr[i][j]
        print('"Expand"')
        self.done = True

    def backup(self, reward):
        """search가 끝나면 지나온 edge의 N과 W를 업데이트."""
        steps = self.action_count
        for i in range(steps):
            # W 배치
            # 내가 지나온 edge에는 v 로
            if self.action_memory[i][0] == PLAYER:
                self.edge_memory[i][tuple(
                    self.action_memory[i][1:])][
                    W] += reward
            # 상대가 지나온 edge는 -v 로
            else:
                self.edge_memory[i][tuple(
                    self.action_memory[i][1:])][
                    W] -= reward
            # N 배치
            self.edge_memory[i][tuple(self.action_memory[i][1:])][N] += 1
            # N, W, Q, P 가 계산된 edge들을 Tree에 최종 업데이트
            self.tree_memory[self.node_memory[i]] = self.edge_memory[i]
        print('"Back Up"')
        self._reset_episode()


if __name__ == "__main__":
    # 시작 시간 측정
    start = time.time()
    # 환경 생성
    env_play = tictactoe_env.TicTacToeEnv()
    env_mcts = tictactoe_env.TicTacToeEnv()
    # 셀프 플레이 인스턴스 생성
    mcts = MCTS()
    # 통계용
    result = {1: 0, 0: 0, -1: 0}
    win_mark_O = 0
    # 게임 시작
    for n_play in range(PLAY):
        # raw state 생성
        state_play = env_play.reset()
        # 플레이어 컬러 환경에 알림
        env_play.player_color = MARK_O
        # Game 시작
        print('=' * 65, '\n{} Game'.format(n_play + 1))
        print("----- GAME -----")
        print(state_play[PLAYER] + state_play[OPPONENT] * 2.0)

        # MCTS 시뮬레이션 시작
        for n_simul in range(SIMULATION):
            state_mcts = env_mcts.reset(state_play.copy())
            env_mcts.player_color = env_play.player_color
            mcts.mark_o = env_play.player_color
            # MCTS 시작
            print('=' * 65, '\n{} Simulation'.format(n_simul + 1))
            # 진행 변수 초기화
            done_simul = False
            while not done_simul:
                # 보드 상황 출력: 내 착수:1, 상대 착수:2
                print("-- SIMULATION --")
                print(state_mcts[PLAYER] + state_mcts[OPPONENT] * 2.0)

                # action 선택하기
                action = mcts.select_action(state_mcts)

                # step 진행
                state_mcts, reward, done, _ = env_mcts.step(action)
                done_simul = mcts.done
            if done_simul:
                # 배업한 보드 보기
                print("---- BACK UP ----")
                print(state_mcts[PLAYER] + state_mcts[OPPONENT] * 2.0)
                print("V:{}\n".format(mcts.value.data.numpy()[0]))
                # 보상을 지나온 edge에 백업
                mcts.backup(mcts.value.data.numpy()[0])
                mcts._reset_step()
            if done:
                # 보상을 지나온 edge에 백업
                mcts.backup(mcts.value.data.numpy()[0])
                mcts._reset_step()

                # 승부 결과 체크
                result[reward] += 1

                # 선공으로 이긴 경우 체크 (밸런스 확인)
                if reward == 1:
                    if env_mcts.player_color == 0:
                        win_mark_O += 1

            # SAVE_CYCLE 마다 Data 저장
            if (n_simul + 1) % SAVE_CYCLE == 0:
                """
                with open('data/state_memory_e{}.pkl'.format(n_simul + 1),
                          'wb') as f:
                    pickle.dump(mcts.state_memory, f,
                    pickle.HIGHEST_PROTOCOL)

                with open('data/tree_memory_e{}.pkl'.format(n_simul + 1),
                          'wb') as f:
                    pickle.dump(mcts.tree_memory, f,
                                pickle.HIGHEST_PROTOCOL)

                # 저장 알림
                print('[{} Episode Data Saved]'.format(n_simul + 1))
                """
                # 종료 시간 측정
                finish = round(float(time.time() - start))

                # 에피소드 통계 문자열 생성
                statics = ('\nWin: {}  Lose: {}  Draw: {}  Winrate: {:0.1f}%  \
            WinMarkO: {}'.format(result[1], result[-1], result[0],
                                 1 / (1 + np.exp(result[-1] / SIMULATION) /
                                      np.exp(result[1] / SIMULATION)) * 100,
                                 win_mark_O))

                # 통계 출력
                print('=' * 65, statics)

                # 슬랙에 보고 메시지 보내기
                slack = slackweb.Slack(
                    url="https://hooks.slack.com/services/T8P0E384U/B8PR44F1C/\
            4gVy7zhZ9teBUoAFSse8iynn")
                slack.notify(
                    text="Finished: {} MCTS in {}s".format(
                        n_simul + 1, finish))
                slack.notify(text=statics)