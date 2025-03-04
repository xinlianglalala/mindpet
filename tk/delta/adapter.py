#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2022-2023, All rights reserved.

from collections import OrderedDict
from tk.utils.version_utils import is_version_ge

import mindspore as ms
import mindspore.nn as nn
import mindspore.common.dtype as mstype
from mindspore.nn.layer.activation import get_activation, _activation
from mindspore.ops import operations as P
from mindspore.ops import functional as F
from tk.delta.delta_constants import VALID_TENSOR_DATATYPE

if is_version_ge(ms.__version__, '2.0.0'):
    from mindformers.modules.layers import Linear, _args_type_validator_check, _valid_value_checks
    import mindspore._checkparam as Validator
    _Linear = Linear
else:
    from mindspore.nn.transformer.layers import _Linear, _args_type_validator_check, _valid_value_checks
    from mindspore._checkparam import Validator
    
class AdapterLayer(nn.Cell):
    """
    定义微调算法adapter layer层，初始化adapter layer层参数，包括矩阵参数、激活层等。

    Args:
        hidden_size (int): 隐藏层输出的特征向量维度。
        bottleneck_size (int): adapter模块中bottleneck的隐藏维度。
        non_linearity (str): adapter模块中bottleneck非线性激活函数，
            str的值引用自mindspore中 `get_activation` 方法所支持的激活函数类型.默认值：'gelu'。
        param_init_type (dtype.Number): 表示dense中模块的参数初始化类型。
        compute_dtype (dtype.Number): 表示dense中矩阵乘法的计算类型。
    """

    @_args_type_validator_check(hidden_size=Validator.check_positive_int,
                                bottleneck_size=Validator.check_positive_int,
                                non_linearity=_valid_value_checks(list(_activation.keys()),
                                                                  "AdapterLayer"),
                                param_init_type=_valid_value_checks(VALID_TENSOR_DATATYPE, "AdapterLayer"),
                                compute_dtype=_valid_value_checks(VALID_TENSOR_DATATYPE, "AdapterLayer"))

    def __init__(
            self,
            hidden_size: int,
            bottleneck_size: int = 64,
            non_linearity: str = "gelu",
            param_init_type: mstype = mstype.float32,
            compute_dtype: mstype = mstype.float16):

        super(AdapterLayer, self).__init__()

        self.bottleneck_size = bottleneck_size
        self.non_linearity_name = non_linearity

        adapter_dict = OrderedDict()
        adapter_dict["tk_delta_adapter_down_sampler"] = _Linear(hidden_size,
                                                                bottleneck_size,
                                                                compute_dtype=compute_dtype,
                                                                param_init_type=param_init_type)
        adapter_dict["tk_delta_adapter_non_linear"] = get_activation(non_linearity)
        adapter_dict["tk_delta_adapter_up_sampler"] = _Linear(bottleneck_size,
                                                              hidden_size,
                                                              compute_dtype=compute_dtype,
                                                              param_init_type=param_init_type)

        self.tk_delta_adapter_block = nn.SequentialCell(adapter_dict)
        self.residual_add = P.Add()
        self.cast = P.Cast()
        self.shape = P.Shape()
        self.reshape = P.Reshape()

    def construct(self, input_tensor):
        # get input_tensor info
        input_tensor_shape = self.shape(input_tensor)
        ori_dtype = F.dtype(input_tensor)

        # reshape input_tensor to compute
        input_tensor = self.reshape(input_tensor, (-1, input_tensor_shape[-1]))

        # calculate adapter_out
        adapter_out = self.tk_delta_adapter_block(input_tensor)

        # residual connection, add input and adapter_out
        output = self.residual_add(input_tensor, adapter_out)

        # recover the previous outshape and dtype
        out_shape = input_tensor_shape[:-1] + (-1, )
        output = self.reshape(output, out_shape)
        output = self.cast(output, ori_dtype)
        return output

    def shard(self,
              strategy_matmul_down_sampler=None,
              strategy_bias_down_sampler=None,
              strategy_non_linearity=None,
              strategy_matmul_up_sampler=None,
              strategy_bias_up_sampler=None,
              strategy_residual_add=None):
        try:
            self.tk_delta_adapter_block.tk_delta_adapter_down_sampler.shard(
                strategy_matmul=strategy_matmul_down_sampler, strategy_bias=strategy_bias_down_sampler)

            if self.non_linearity_name.lower() == "leakyrelu":
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.select_op.shard(
                    (strategy_non_linearity[0], strategy_non_linearity[0]))
            elif self.non_linearity_name.lower() == "logsigmoid":
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.mul.shard((strategy_non_linearity[0], ()))
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.exp.shard(strategy_non_linearity)
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.add.shard((strategy_non_linearity[0], ()))
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.rec.shard(strategy_non_linearity)
                self.tk_delta_adapter_block.tk_delta_adapter_non_linear.log.shard(strategy_non_linearity)
            elif self.non_linearity_name.lower() == "logsoftmax":
                raise ValueError("The 'LogSoftmax' function is not supported in semi auto parallel "
                                "or auto parallel mode.")
            else:
                getattr(self.tk_delta_adapter_block.tk_delta_adapter_non_linear, 
                    self.non_linearity_name).shard(strategy_non_linearity)

            self.tk_delta_adapter_block.tk_delta_adapter_up_sampler.shard(strategy_matmul=strategy_matmul_up_sampler,
                                                                        strategy_bias=strategy_bias_up_sampler)

            self.residual_add.shard(strategy_residual_add)

        except Exception as ex:
            raise Exception(f"Exception occurred when set the shard for AdapterLayer, error message: \
                {str(ex)}") from ex
        

class AdapterDense(nn.Dense):
    """
    定义微调算法adapter dense层，继承nn.Dense。

    Args:
        in_channels (int): adapter dense层输入Tensor的空间维度。
        out_channels (int): adapter dense层输出Tensor的空间维度。
        weight_init (Union[Tensor, str, Initializer, numbers.Number]):
            线性层权重参数的初始化方法。
            它的类型可以是Tensor，str，Initializer或numbers.Number。
            当使用str时，值引用自类initializer；更多细节请参考Initializer的值。
            当使用Tensor时，数据类型与输入Tensor相同。
            默认值："normal"。
        bias_init (Union[Tensor, str, Initializer, numbers.Number]): 
            线性层偏置参数的初始化方法。
            它的类型可以是Tensor，str，Initializer或numbers.Number。
            当使用str时，值引用自类initializer；更多细节请参考Initializer的值。
            当使用Tensor时，数据类型与输入Tensor相同。
            默认值："zeros"。
        has_bias (int): 是否有偏置。
        activation (Union[str, Cell, Primitive, None]): 激活函数，是与创建层的输入具有相同数据类型的权重矩阵。
        bottleneck_size (int): adapter模块中bottleneck的隐藏大小，默认值：64。
        non_linearity (str): adapter模块中bottleneck非线性激活函数，
            str的值引用自mindspore中 `get_activation` 方法所支持的激活函数类型.默认值：'gelu'。
        param_init_type (dtype.Number): 表示dense中模块的参数初始化类型。
            其值应为dtype.float32或dtype.float16。
            默认值：dtype.float32。
        compute_dtype (dtype.Number): 表示dense中矩阵乘法的计算类型。
            其值应为dtype.float32或dtype.float16。
            默认值：dtype.float16。

    Inputs:
        x (Tensor): 网络的所有输入组成的元组，shape为(*, in_channels)的Tensor，
            in_channels与入参中的in_channels类型一致。
    Outputs:
        output (Tensor): adapter dense的计算结果，shape为 (*, out_channels)，
            out_channels与入参中的out_channels类型一致。
    """

    @_args_type_validator_check(in_channels=Validator.check_positive_int,
                                out_channels=Validator.check_positive_int,
                                has_bias=Validator.check_bool,
                                bottleneck_size=Validator.check_positive_int,
                                non_linearity=_valid_value_checks(list(_activation.keys()), "AdapterDense"),
                                param_init_type=_valid_value_checks(VALID_TENSOR_DATATYPE, "AdapterDense"),
                                compute_dtype=_valid_value_checks(VALID_TENSOR_DATATYPE, "AdapterDense"))

    def __init__(self,
                 in_channels: int,
                 out_channels: int,
                 weight_init="normal",
                 bias_init="zeros",
                 has_bias: bool = True,
                 activation=None,
                 bottleneck_size: int = 64,
                 non_linearity: str = "gelu",
                 param_init_type: mstype = mstype.float32,
                 compute_dtype: mstype = mstype.float16,
                 **kwargs):

        super(AdapterDense, self).__init__(in_channels=in_channels,
                                           out_channels=out_channels,
                                           weight_init=weight_init,
                                           bias_init=bias_init,
                                           has_bias=has_bias,
                                           activation=activation)

        self.tk_delta_adapter = AdapterLayer(hidden_size=out_channels,
                                             bottleneck_size=bottleneck_size,
                                             non_linearity=non_linearity,
                                             param_init_type=param_init_type,
                                             compute_dtype=compute_dtype)

        self.bottleneck_size = bottleneck_size
        self.compt_dtype = compute_dtype
        self.cast = P.Cast()
        self.act_name = activation

    def construct(self, input_tensor):

        # get input_x info
        x_shape = self.shape_op(input_tensor)
        ori_dtype = F.dtype(input_tensor)

        # reshape input_tensor to compute
        input_tensor = self.reshape(input_tensor, (-1, x_shape[-1]))

        # start to linear compute
        weight = self.cast(self.weight, self.compt_dtype)
        input_tensor = self.cast(input_tensor, self.compt_dtype)

        input_tensor = self.matmul(input_tensor, weight)
        if self.has_bias:
            input_tensor = self.bias_add(
                input_tensor, self.cast(self.bias, self.compt_dtype))
        if self.activation_flag:
            input_tensor = self.activation(input_tensor)

        # calculate adapter_out
        input_tensor = self.tk_delta_adapter(input_tensor)

        # recover the previous outshape and dtype
        out_shape = x_shape[:-1] + (-1,)
        input_tensor = self.reshape(input_tensor, out_shape)
        output = self.cast(input_tensor, ori_dtype)
        return output

    def shard(self, 
              strategy_matmul_org=None,
              strategy_bias_org=None,
              strategy_activation_org=None,
              strategy_matmul_down_sampler=None,
              strategy_bias_down_sampler=None,
              strategy_non_linearity=None,
              strategy_matmul_up_sampler=None,
              strategy_bias_up_sampler=None,
              strategy_residual_add=None):
        try:
            # set origin dense strategy
            self.matmul.shard(strategy_matmul_org)
            if self.has_bias:
                self.bias_add.shard(strategy_bias_org)
            if self.activation_flag and isinstance(self.act_name, str):
                if self.act_name.lower() == "leakyrelu":
                    self.activation.select_op.shard(
                        (strategy_activation_org[0], strategy_activation_org[0]))
                elif self.act_name.lower() == "logsigmoid":
                    self.activation.mul.shard((strategy_activation_org[0], ()))
                    self.activation.exp.shard(strategy_activation_org)
                    self.activation.add.shard((strategy_activation_org[0], ()))
                    self.activation.rec.shard(strategy_activation_org)
                    self.activation.log.shard(strategy_activation_org)
                elif self.act_name.lower() == "logsoftmax":
                    raise ValueError("The 'LogSoftmax' function is not supported in semi auto parallel "
                                    "or auto parallel mode.")
                else:
                    getattr(self.activation, self.act_name).shard(strategy_activation_org)

            # set adapter strategy
            self.tk_delta_adapter.shard(strategy_matmul_down_sampler=strategy_matmul_down_sampler,
                                        strategy_bias_down_sampler=strategy_bias_down_sampler,
                                        strategy_non_linearity=strategy_non_linearity,
                                        strategy_matmul_up_sampler=strategy_matmul_up_sampler,
                                        strategy_bias_up_sampler=strategy_bias_up_sampler,
                                        strategy_residual_add=strategy_residual_add)

        except Exception as ex:
            raise Exception(f"Exception occurred when set the shard for AdapterDense, error message: \
                {str(ex)}") from ex
