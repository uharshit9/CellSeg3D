from monai.networks.nets import SegResNetVAE


def get_net():

    return SegResNetVAE(
        input_image_size=[128, 128, 128], out_channels=1, dropout_prob=0.1
    )


def get_weights_file():
    return "SegResNet.pth"


def get_output(model, input):
    out = model(input)
    return out[0]


def get_validation(model, val_inputs):
    val_outputs = model(val_inputs)
    return val_outputs[0]
