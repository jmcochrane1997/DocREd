import os, time
from tabnanny import check
import random
import numpy as np
import torch
#from apex import amp
import ujson as json
from torch.utils.data import DataLoader
from transformers import AutoConfig, AutoModel, AutoTokenizer
from transformers.optimization import AdamW, get_linear_schedule_with_warmup
from model import DocREModel
from utils import set_seed, collate_fn, Logger
from prepro import read_docred
from evaluation import to_official, official_evaluate, macro_evaluate
from config import get_args
from torch.utils.tensorboard import SummaryWriter


def train(args, model, train_features, dev_features, test_features, id2rel, logger):
    def finetune(features, optimizer, num_epoch, num_steps, id2rel, logger):
        best_score = -1
        best_score_ign = -1
        best_epoch = -1
        best_threshold = 0.0
        train_dataloader = DataLoader(features, batch_size=args.train_batch_size, shuffle=True, 
                                      collate_fn=collate_fn, drop_last=True)
        train_iterator = range(int(num_epoch))
        total_steps = int(len(train_dataloader) * num_epoch // args.gradient_accumulation_steps)
        warmup_steps = int(total_steps * args.warmup_ratio)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=warmup_steps, 
                                                    num_training_steps=total_steps)
        logger.write("Total steps: {}\n".format(total_steps))
        logger.write("Warmup steps: {}\n".format(warmup_steps))
        for epoch in train_iterator:


            ####################################
            print("EPOCH "+str(epoch))
            ####################################

            model.zero_grad()
            start_time = time.time()
            for step, batch in enumerate(train_dataloader):



                ####################################
                print("STEP: "+str(step))
                ####################################



                model.train()
                outputs = model(**batch)

                re_loss = outputs["re_loss"]
                loss = re_loss
                correl_rel_loss = torch.tensor(0.0)
                correl_triplet_loss = torch.tensor(0.0) 

                if args.joint_label_embed and args.rel_correl and not args.triplet_correl:
                    correl_rel_loss = outputs["correl_rel_loss"]
                    loss = (1+0.25) * loss * correl_rel_loss / (0.25 * correl_rel_loss + loss)

                if args.triplet_correl and not (args.joint_label_embed and args.rel_correl):
                    correl_triplet_loss = outputs["correl_triplet_loss"]
                    loss = (1+0.25) * loss * correl_triplet_loss / (0.25 * correl_triplet_loss + loss)

                if args.triplet_correl and args.joint_label_embed and args.rel_correl:
                    correl_rel_loss = outputs["correl_rel_loss"]
                    correl_triplet_loss = outputs["correl_triplet_loss"]
                    correl_loss = correl_rel_loss * alpha + correl_triplet_loss * (1-alpha)
                    loss = (1+0.25) * loss * correl_loss / (0.25 * correl_loss + loss)


                loss = (loss) / args.gradient_accumulation_steps

                # with amp.scale_loss(loss, optimizer) as scaled_loss:
                #     scaled_loss.backward()
                loss.backward()
                if step % args.gradient_accumulation_steps == 0:
                    if args.max_grad_norm > 0:
                        # torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
                    optimizer.step()
                    scheduler.step()
                    model.zero_grad()
                    num_steps += 1
                
                gap = 150 
                if step%gap == 0:
                    logger.write('{:3d} step/Epoch{:3d}, Total Loss {:8f}, DocRE_loss {:8f}, RelCorrel_loss {:8f}, TriCorel_loss {:8f},\n'.format(step, epoch, loss.item(), re_loss.item(), correl_rel_loss.item(), correl_triplet_loss.item()))

                if (step + 1) == len(train_dataloader) - 1 or (args.evaluation_steps > 0 and num_steps % args.evaluation_steps == 0 and step % args.gradient_accumulation_steps == 0):
                    logger.write('| epoch {:3d} | time: {:5.2f}s \n'.format(epoch, time.time() - start_time))

                    eval_start_time = time.time()
                    dev_score, best_thres, dev_output = evaluate(args, model, dev_features, id2rel, logger, tag="dev", g_threshold=g_threshold, macro=False, use_g_thres=False)
                    logger.write(json.dumps(dev_output) + "\n")
    
                    if dev_score > best_score:
                        best_score = dev_score
                        best_score_ign = dev_output["dev_F1_ign"]
                        best_epoch = epoch
                        best_threshold = best_thres
                        
                        if args.model_prefix != "":
                            save_path = os.path.join(args.save_path, args.model_prefix + "-" + str(args.seed)) + ".pt"
                            torch.save(model.state_dict(), save_path)
                            logger.write("best model saved!\n")
                        
                    logger.write('| epoch {:3d} | time: {:5.2f}s | best epoch:{:3d} Ign F1:{:5.3f}% F1:{:5.3f}% Threshold: {:5.3f}\n'.format(epoch, time.time() - eval_start_time, best_epoch, best_score_ign,  best_score*100, best_threshold))
        logger.write('seed:{:3d} | best epoch:{:3d} Ign F1:{:5.3f}% F1:{:5.3f}%'.format(args.seed, best_epoch*100, best_score_ign,  best_score*100))
        logger.write(f' | {save_path.split("/")[-1]}\n')
        return num_steps

    re_layer = ["extractor", "bilinear", "classifier", "joint", "correl_rel", "correl_triplet"]
    graph_layer = ["graph", ]

    plms_parameters = []
    for n, p in model.named_parameters():
        if (not any(nd in n for nd in re_layer)) and (not any(nd in n for nd in graph_layer)):
            plms_parameters.append(p)

    optimizer_grouped_parameters = [
        {"params": plms_parameters, },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in re_layer)], "lr": 1e-4},
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in graph_layer)], "lr": 1e-3},
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    # model, optimizer = amp.initialize(model, optimizer, opt_level="O1", verbosity=0)  # O0
    num_steps = 0
    set_seed(args)
    model.zero_grad()
    alpha = args.alpha
    print(f"alpha: {alpha}")
    g_threshold=0.5
    # print(f"g_threshold: {g_threshold}")
    finetune(train_features, optimizer, args.num_train_epochs, num_steps, id2rel, logger)


def evaluate(args, model, features, id2rel, logger, tag="dev", g_threshold=None, macro=False, use_g_thres=False):

    dataloader = DataLoader(features, batch_size=args.test_batch_size, shuffle=False, collate_fn=collate_fn, drop_last=False)
    preds = []
    for batch in dataloader:
        model.eval()

        with torch.no_grad():
            batch.pop('labels')
            outputs = model(**batch)
            pred = outputs["logits"]
            preds.append(pred)
    
    preds = torch.cat(preds, dim=0)

    if tag == "dev":

        best_f1 = 0.0
        best_f1_ign = 0.0
        best_threshold = 0.0

        if not use_g_thres:
            global_threshold = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
            logger.write("-"*80)
            logger.write("\nEvaluation with different global thereshold ...\n")
            for threshold in global_threshold:
                cur_output = get_output_labels(preds, theta=threshold).cpu().numpy()
                cur_output[np.isnan(cur_output)] = 0
                cur_output = cur_output.astype(np.float32)

                ans = to_official(cur_output, features, id2rel)
                cur_f1 = 0.0
                cur_f1_ign = 0.0
                if len(ans) > 0:
                    cur_f1, _, cur_f1_ign, _ = official_evaluate(ans, args.data_dir, eval=tag)
                cur_output = {
                    tag + "_F1": cur_f1 * 100,
                    tag + "_F1_ign": cur_f1_ign * 100,
                }
                logger.write(f"[Threshold {threshold}]: ")
                logger.write(json.dumps(cur_output) + "\n")
                if cur_f1 > best_f1:
                    best_f1 = cur_f1
                    best_f1_ign = cur_f1_ign
                    best_threshold = threshold
            logger.write("-"*80 + "\n")
        
        else:

            logger.write("-"*80)
            logger.write("\nEvaluation with different global thereshold ...\n")
            cur_output = get_output_labels(preds, theta=g_threshold).cpu().numpy()
            cur_output[np.isnan(cur_output)] = 0
            cur_output = cur_output.astype(np.float32)

            ans = to_official(cur_output, features, id2rel)
            cur_f1 = 0.0
            cur_f1_ign = 0.0
            if len(ans) > 0:
                cur_f1, _, cur_f1_ign, _ = official_evaluate(ans, args.data_dir, eval=tag)
            
            best_f1 = cur_f1
            best_f1_ign = cur_f1_ign
            best_threshold = g_threshold
            logger.write("-"*80 + "\n")

        output = {
            tag + "_F1": best_f1 * 100,
            tag + "_F1_ign": best_f1_ign * 100,
            tag + "_Threshold": best_threshold,
        }
        if macro:
            macro_output = evaluate_macro(args, preds, best_threshold, features, id2rel, tag=tag, logger=logger)

        return best_f1, best_threshold, output

def report(args, model, features, id2rel, g_threshold=None):

    dataloader = DataLoader(features, batch_size=args.test_batch_size, shuffle=False, collate_fn=collate_fn, drop_last=False)
    preds = []
    for batch in dataloader:
        model.eval()

        with torch.no_grad():
            batch.pop('labels')
            outputs = model(**batch)
            pred = outputs["logits"]
            preds.append(pred)

    preds = torch.cat(preds, dim=0)
    cur_output = get_output_labels(preds, theta=g_threshold).cpu().numpy()
    cur_output[np.isnan(cur_output)] = 0
    preds = cur_output.astype(np.float32)

    preds = to_official(preds, features, id2rel)
    return preds

def get_output_labels(logits, theta=0.):
    output = torch.zeros_like(logits).to(logits.device) 
    mask = (torch.sigmoid(logits)>theta)
    output[mask] = 1.0
    output[:, 0] = (output.sum(dim=1) == 0).to(logits.device)
    return output

def evaluate_macro(args, preds, best_threshold, features, id2rel, tag="dev", logger=None):
    cur_output = get_output_labels(preds, theta=best_threshold).cpu().numpy()
    cur_output[np.isnan(cur_output)] = 0
    cur_output = cur_output.astype(np.float32)

    ans = to_official(cur_output, features, id2rel)
    micro_f1, macro_f1_all, macro_f1_500, macro_f1_200, macro_f1_100 = 0, 0, 0, 0, 0
    if len(ans) > 0:
        micro_f1, macro_f1_all, macro_f1_500, macro_f1_200, macro_f1_100, macro_f1_greater_500 = \
            macro_evaluate(ans, args.data_dir, eval=tag)
    cur_output = {
        tag + "_micro_f1": micro_f1 * 100,
        tag + "_macro_f1_all": macro_f1_all * 100,
        tag + "_macro_f1_500": macro_f1_500 * 100,
        tag + "_macro_f1_200": macro_f1_200 * 100,
        tag + "_macro_f1_100": macro_f1_100 * 100,
        tag + "_macro_f1_over_500": macro_f1_greater_500 * 100,
    }
    logger.write(json.dumps(cur_output, indent=4) + "\n")
    return cur_output


def main():
    args = get_args()
    random_seed = random.randint(10, 100)
    # args.seed = random_seed

    if args.load_path == "":
        log_file = "Train-" + args.data_dir.split("/")[-1] + "-" + args.model_prefix + "-" + str(args.seed) + ".log"
        logger = Logger(file_name=log_file, log=True)
    else:
        log_file = "Eval-" + args.data_dir.split("/")[-1] + "-" + args.load_path.split("/")[-1].split(".")[0] + ".log"
        logger = Logger(file_name=log_file, log=False)

    logger.write(json.dumps(args.__dict__, indent=4))
    logger.write("\n")

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    args.n_gpu = torch.cuda.device_count()
    args.device = device
    
    set_seed(args)

    config = AutoConfig.from_pretrained(
        args.config_name if args.config_name else args.model_name_or_path,
        num_labels=args.num_class,
    )

    #####################################################
   # print("CONFIG...")
   # print(config)
    #####################################################


    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
    )

   

    if args.joint_label_embed:  # joint learn label embeddings
        rel_list = []
        for i in range(args.num_class):
            rel_list.append("[rel-" + str(i) + "]")
        tokenizer.add_tokens(rel_list, special_tokens=True)


    #####################################################
    print("TOKENIZER...")
    print(tokenizer)
    #####################################################

    rel2id = json.load(open(os.path.join(args.data_dir, 'rel2id.json'), 'r'))
    id2rel = {value: key for key, value in rel2id.items()}
    read = read_docred

    train_file = os.path.join(args.data_dir, args.train_file)
    dev_file = os.path.join(args.data_dir, args.dev_file)
    test_file = os.path.join(args.data_dir, args.test_file)

    train_file_out = os.path.join(args.prepro_dir, args.transformer_type + "_" + args.train_file)
    dev_file_out = os.path.join(args.prepro_dir, args.transformer_type + "_" + args.dev_file)
    test_file_out = os.path.join(args.prepro_dir, args.transformer_type + "_" + args.test_file)

    train_features = read(args, train_file, train_file_out, tokenizer, rel2id, max_seq_length=args.max_seq_length, logger=logger)
    dev_features = read(args, dev_file, dev_file_out, tokenizer, rel2id, max_seq_length=args.max_seq_length, logger=logger)
    test_features = read(args, test_file, test_file_out, tokenizer, rel2id, max_seq_length=args.max_seq_length, logger=logger)


    model = AutoModel.from_pretrained(
        args.model_name_or_path,
        from_tf=bool(".ckpt" in args.model_name_or_path),
        config=config,
    )

    if args.joint_label_embed:
        model.resize_token_embeddings(len(tokenizer))

    config.cls_token_id = tokenizer.cls_token_id
    config.sep_token_id = tokenizer.sep_token_id
    config.transformer_type = args.transformer_type
    config.max_seq_length = args.max_seq_length
    config.num_class = args.num_class
    config.use_graph = args.use_graph
    config.graph_type = args.graph_type
    config.device = args.device
    config.triplet_correl = args.triplet_correl
    config.rel_correl = args.rel_correl
    config.joint_label_embed = args.joint_label_embed
    

    model = DocREModel(config, model, num_labels=args.num_labels)
    #model.to(0)
    
    
    logger.write('total parameters:' + str(sum([np.prod(list(p.size())) for p in model.parameters() if p.requires_grad])) + "\n")

    if args.load_path == "":  # Training
        train(args, model, train_features, dev_features, test_features, id2rel, logger)
    else:  # Testing
        args.load_path = os.path.join(args.save_path, args.load_path)
        check_point = args.load_path.split("/")[-1]
        logger.write(f"evaluation begins for checkpoint: {check_point}\n")
        start_time = time.time()
        # model = amp.initialize(model, opt_level="O1", verbosity=0)
        model.load_state_dict(torch.load(args.load_path), strict=False)
        
        dev_score, best_thres, dev_output = evaluate(args, model, dev_features, id2rel, logger, tag="dev", macro=True)
        logger.write(json.dumps(dev_output) + " | time: " + str(time.time()-start_time) + "s\n")

        # logger.write("evaluation on test set ...\n")
        # pred = report(args, model, test_features, id2rel, g_threshold=best_thres)
        # result_file = "result.json"
        # with open(result_file, "w") as fh:
        #     json.dump(pred, fh)
        # logger.write("The result file of test set is generated and saved!\n")


if __name__ == "__main__":
    main()
